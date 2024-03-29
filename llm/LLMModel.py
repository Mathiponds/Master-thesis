from abc import ABC, abstractmethod
from datetime import datetime
import torch
import time
from typing import Any

from tqdm import tqdm
import pandas as pd
import os
import spacy
import transformers
from transformers import AutoTokenizer

from ner.Datasets.Conll2003Dataset import get_test_cleaned_split as conll_get_test_cleaned_split
from ner.Datasets.OntoNotes5Dataset import get_test_cleaned_split as ontonote_get_test_cleaned_split, ONTONOTE5_TAGS

from ner.llm_ner.ResultInstance import ResultInstance, ResultInstanceWithConfidenceInterval, save_result_instance_with_CI
from ner.llm_ner.confidence_checker import ConfidenceChecker
from ner.llm_ner.verifier import Verifier
from ner.llm_ner.few_shots_techniques import *
from ner.llm_ner.prompt_techniques.pt_filing import PT_Filing
from ner.llm_ner.prompt_techniques.pt_abstract import PromptTechnique
from ner.llm_ner.prompt_techniques.pt_discussion import PT_OutputList
from ner.llm_ner.prompt_techniques.pt_gpt_ner import PT_GPT_NER
from ner.llm_ner.prompt_techniques.pt_wrapper import PT_Wrapper
from ner.llm_ner.prompt_techniques.pt_multi_pt import PT_Multi_PT, PT_2Time_Tagger
from ner.llm_ner.prompt_techniques.pt_tagger import PT_Tagger
from ner.llm_ner.prompt_techniques.pt_get_entities import PT_GetEntities

from llm.llm_finetune import load_model_tokenizer_for_inference, load_model_tokenizer_for_training, split_train_test, tokenize_prompt

from ner.utils import run_command, latex_escape
from ner.llm_ner.prompts import prompt_template, prompt_template_ontonotes

from llm.LlamaLoader import LlamaLoader, Llama_LlamaCpp, Llama_Langchain

class LLMModel(ABC):
    def __init__(self, base_model_id, 
                 base_model_name, 
                 check_nb_tokens = True, 
                 max_tokens = 256, 
                 quantization = "Q5_0", 
                 llm_loader : LlamaLoader = None, 
                 without_model = False,
                 lora_path = None) -> None:
        self.base_model_id = base_model_id
        self.base_model_name = base_model_name.lower()
        if not lora_path :
            self.name = self.base_model_name
        else :
            model_info = lora_path.split('/')
            self.name = f"{self.base_model_name}-ft-{model_info[-2].split('-')[1]}-{model_info[-1].split('-')[1]}-{quantization}"

        self.max_tokens = max_tokens
        if not llm_loader :
            llm_loader = Llama_LlamaCpp
        self.llm_loader = llm_loader()
        
        if not without_model :
            self.model : LlamaLoader = self.get_model(quantization = quantization, lora_path = lora_path)
        self.check_nb_tokens = check_nb_tokens
        if check_nb_tokens :
            self.nlp = spacy.load("en_core_web_sm")  # Load a spaCy language model
            
    
    def get_model(self, quantization = 'Q5_0', gguf_model_path = "" ,lora_path = None):
        if gguf_model_path :
            model_path = gguf_model_path
        elif not quantization : 
            model_path = self.base_model_id
        else :
            model_path = f"llm/models/{self.base_model_name}/{self.base_model_name}.{quantization}.gguf"
        self.model = None
        self.model = self.llm_loader.get_llm_instance(model_path = model_path, lora_path= lora_path)
        with torch.no_grad():
            torch.cuda.empty_cache()
        return self.model
        

    def __str__(self) -> str:
        return self.name

    def __call__(self, prompt, with_full_message = False) -> Any:
        return self.model(prompt, with_full_message)
        # return prompt
    
    def invoke_mulitple(self, sentences : list[str], pt : PromptTechnique, verifier : Verifier, confidence_checker : ConfidenceChecker, tags = ["PER", "ORG", "LOC", 'MISC']):
        all_entities = []
        for sentence in tqdm(sentences) :
            all_entities.append(self.invoke(sentence, pt, verifier, confidence_checker, tags)[0])
        return all_entities
    
    
    def invoke(self, sentence : str, pt : PromptTechnique, verifier : Verifier, confidence_checker : ConfidenceChecker, tags):
        all_entities, response_all= pt.run_prompt(self, sentence, verifier, confidence_checker, tags = tags)
        return all_entities, response_all
    
    @staticmethod
    def show_prompts(pts : list[PromptTechnique] = [PT_Filing, PT_OutputList, PT_Wrapper, PT_Tagger, PT_GetEntities, PT_GPT_NER],
                       nb_few_shots = [5], verifier = False, dataset_loader = ontonote_get_test_cleaned_split,
                       tags = ONTONOTE5_TAGS, with_gold = True) :
        
        data_train, data_test = dataset_loader()
        fst = FST_Sentence(data_train)
        for i_pt in pts : 
            print()
            print(f"------------{i_pt.name()}-------------------------")
            for plus_plus in [False, True] :
                print(f'---------------------{"prompt++" if plus_plus else "raw"}---------------------')
                print()
                pt = i_pt(fst, with_precision = False, prompt_template = prompt_template_ontonotes, plus_plus = plus_plus)
                gold = pt.get_gold(data_test, tag = tags[0])
                print(f"{latex_escape(pt.get_prompts_runnable(data_test[0]['text'], tags[0][0]))} {gold[0] if with_gold else ''}")
                print("----------------------------------------------------")

    def classical_test_ontonote5(self, 
                       fsts : list[FewShotsTechnique]= [FST_NoShots, FST_Sentence], 
                       pts : list[PromptTechnique] = [PT_GPT_NER, PT_OutputList, PT_Wrapper],
                       nb_few_shots = [5], 
                       verifier = False, 
                       confidence_checker = False, 
                       save = True, 
                       nb_run_by_test = 3,
                       with_precision = False,
                       prompt_template = prompt_template_ontonotes,
                       plus_plus = False,
                       dataset_loader = ontonote_get_test_cleaned_split,
                       test_size = 100) :
        return self.classical_test(fsts , 
                       pts,
                       nb_few_shots, 
                       verifier, 
                       confidence_checker, 
                       save, 
                       nb_run_by_test ,
                       with_precision ,
                       prompt_template,
                       plus_plus,
                       dataset_loader = lambda seed = 42 : dataset_loader(seed = seed, test_size = test_size),
                       tags = ONTONOTE5_TAGS,
                       dataset_save_name = "ontonote5")
    

    def classical_test(self, 
                       fsts : list[FewShotsTechnique]= [FST_NoShots, FST_Sentence, FST_Entity, FST_Random], 
                       pts : list[PromptTechnique] = [PT_GPT_NER, PT_OutputList, PT_Wrapper],
                       nb_few_shots = [5], 
                       verifier = False, 
                       confidence_checker = False, 
                       save = True, 
                       nb_run_by_test = 3,
                       with_precision = False,
                       prompt_template = prompt_template,
                       plus_plus = False,
                       dataset_loader = conll_get_test_cleaned_split,
                       tags = ["PER", "ORG", "LOC", 'MISC'],
                       dataset_save_name = "conll2003_cleaned") :

        verifier = Verifier(self, data_train) if verifier else None
        confidence_checker = ConfidenceChecker() if confidence_checker else None

        results : list[ResultInstanceWithConfidenceInterval] = []

        for n in nb_few_shots :
            fsts_i : list[FewShotsTechnique]= [fst(None, n) for fst in fsts]
            for fst in fsts_i :
                print(f"Testing with {fst}")
                pts_i : list[PromptTechnique] = [pt(fst, with_precision = with_precision, prompt_template=prompt_template, plus_plus=plus_plus) for pt in pts]
                for pt in pts_i :
                    print(f"      and {pt}")
                    res_insts = []
                    for run in range(nb_run_by_test) :
                        start_time = time.time()
                        seed = [42, 45, 46, 43, 42,41][run]# random.randint(0, 1535468)
                        data_train, data_test = dataset_loader(seed = seed)
                        fst.set_dataset(data_train)
                        predictions = self.invoke_mulitple(data_test['text'], pt, verifier, confidence_checker, tags)
                        # Calculate the elapsed time
                        elapsed_time = time.time() - start_time
                        res_insts.append(ResultInstance(
                            model= str(self),
                            nb_few_shots = n,
                            noshots = 'noshots' in str(self),
                            prompt_technique = str(pt),
                            few_shot_tecnique = str(fst),
                            verifier = str(verifier),
                            results = predictions,
                            gold = data_test['spans'],
                            data_test = data_test,
                            data_train = data_train,
                            elapsed_time = elapsed_time,
                            with_precision = pt.with_precision,
                            plus_plus = plus_plus,
                            seed = seed,
                            tags = tags
                        ))
                        del data_test, data_train
                    results.append(ResultInstanceWithConfidenceInterval(res_insts))
                    if save :
                        save_result_instance_with_CI(results[-1], dataset = dataset_save_name)
                    fst.save_few_shots()
        results_df = pd.DataFrame([result.get_dict() for result in results])
        return results, results_df

    def classical_test_multiprompt(self, pt : PT_Multi_PT,
                       nb_few_shots = [3], verifier = False, confidence_checker = True, save = True, nb_run_by_test = 10, dataset_loader = conll_get_test_cleaned_split) :
        
        verifier = Verifier(self, data_train) if verifier else None
        confidence_checker = ConfidenceChecker() if confidence_checker else None

        results : list[ResultInstanceWithConfidenceInterval] = []

        res_insts = []
        fst : FewShotsTechnique = pt.pts[0].fst
        for _ in range(nb_run_by_test) :
            start_time = time.time()
            seed = random.randint(0, 1535468)
            data_train, data_test = dataset_loader(seed = seed)
            fst.set_dataset(data_train)
            predictions = self.invoke_mulitple(data_test['text'], pt, verifier, confidence_checker)
            # Calculate the elapsed time
            elapsed_time = time.time() - start_time
            res_insts.append(ResultInstance(
                model= str(self),
                nb_few_shots = fst.nb_few_shots,
                prompt_technique = str(pt),
                few_shot_tecnique = str(fst),
                verifier = str(verifier),
                results = predictions,
                gold = data_test['spans'],
                data_test = data_test,
                data_train = data_train,
                elapsed_time = elapsed_time,
                with_precision = pt.with_precision,
                seed = seed,
            ))
            del data_test, data_train
        results.append(ResultInstanceWithConfidenceInterval(res_insts))
        if save :
            save_result_instance_with_CI(results[-1])
        results_df = pd.DataFrame([result.get_dict() for result in results])
        return results, results_df
    
    @staticmethod
    def finetune(pt: PromptTechnique, dataset = None, base_model_id = "mistralai/Mistral-7B-v0.1", runs = 2000, cleaned = False, precision = None, checkpoint = None, testing = False):
        processed_dataset = pt.load_processed_dataset(runs, cleaned= cleaned, precision=precision, dataset = dataset)
        nb_samples = len(processed_dataset) 
        output_dir = f"./llm/models/{base_model_id.split('/')[-1].lower()}/{pt.__str__()}{f'-{precision}' if precision else ''}/finetuned-{nb_samples}"
        test_size = 50 if not testing else 2
        train_size = nb_samples-test_size if not testing else 2
        base_model, tokenizer = load_model_tokenizer_for_training(base_model_id)
        tokenized_dataset = processed_dataset.map(lambda row : tokenize_prompt(row, tokenizer))
        tokenized_train_dataset, tokenized_val_dataset = split_train_test(tokenized_dataset, train_size, test_size = test_size)

        trainer = transformers.Trainer(
            model=base_model,
            train_dataset=tokenized_train_dataset,
            eval_dataset=tokenized_val_dataset,
            args=transformers.TrainingArguments(
                output_dir=output_dir,
                overwrite_output_dir = True,
                warmup_steps=1,
                per_device_train_batch_size=4,
                gradient_accumulation_steps=1,
                num_train_epochs = 1,
                learning_rate=2.5e-4, # Want a small lr for finetuning
                
                # bf16=True,
                optim="paged_adamw_8bit",
                logging_dir="./logs",        # Directory for storing logs

                save_strategy="steps",       # Save the model checkpoint every logging step
                save_steps=min(runs/4//4,2000),                # Save checkpoints every 50 steps
                evaluation_strategy="steps", # Evaluate the model every logging step
                eval_steps=min(runs/4//8, 500//4),               # Evaluate and save checkpoints every 50 steps
                save_safetensors = False,
                do_eval=True,                # Perform evaluation at the end of training
                # report_to="wandb",           # Comment this out if you don't want to use weights & baises
                run_name=f"finetuned-{pt.__str__}-{nb_samples}-{datetime.now().strftime('%Y-%m-%d-%H-%M')}"          # Name of the W&B run (optional)
            ),
            data_collator=transformers.DataCollatorForLanguageModeling(tokenizer, mlm=False),
        )

        base_model.config.use_cache = False  # silence the warnings. Please re-enable for inference!
        if checkpoint : 
            trainer.train(resume_from_checkpoint = True)
        trainer.train()
        trainer.save_model()

    def load_finetuned_model(self,pt, prompt_type_name, nb_samples = 2000, quantization = "Q5_0", precision = None):
        path_to_lora = f"./llm/models/{self.base_model_name}/{pt.__str__()}-{prompt_type_name}{f'-{precision}' if precision else ''}/finetuned-{nb_samples}"
        model_out = f"{path_to_lora}/model-{quantization}.gguf"

        if not os.path.exists(model_out):
            model_type = 'llama' #llama, starcoder, falcon, baichuan, or gptneox
            command = f"python3 llama.cpp/convert-lora-to-ggml.py {path_to_lora}"
            print(command)
            run_command(command)


            model_base = f"./llm/models/{self.base_model_name}/{self.base_model_name}.{quantization}.gguf"
            lora_scaled = f"{path_to_lora}/ggml-adapter-model.bin"

            # model_out = f"{path_to_lora}/llama-13b-finetuned-2000-v0.gguf"
            # lora_scaled = f"{path_to_lora}/ggml-adapter-model.bin"

            command = f"llama.cpp/export-lora --model-base {model_base} --model-out {model_out} --lora-scaled {lora_scaled} 1.0"
            print(command)

            run_command(command)

        self.name = f"{self.base_model_name}-ft-{prompt_type_name}-{nb_samples}-{quantization}{f'-{precision}' if precision else ''}"
        self.get_model(gguf_model_path =  model_out)
        return self.model
    
    def set_grammar(self, type_of_grammar):
        self.llm_loader.set_grammar(type_of_grammar)

class Llama13b(LLMModel):
    def __init__(self, base_model_id = "meta-llama/Llama-2-13b-hf", base_model_name = "Llama-2-13b", llm_loader = None, without_model = False) -> None:
        super().__init__(base_model_id, base_model_name, llm_loader=llm_loader, without_model=without_model)
    
    @staticmethod
    def name():
        return "Llama-2-13b".lower()


class Llama7b(LLMModel):
    def __init__(self, base_model_id = "meta-llama/Llama-2-7b-hf", base_model_name = "Llama-2-7b", llm_loader = None, without_model = False) -> None:
        super().__init__(base_model_id, base_model_name, llm_loader=llm_loader, without_model=without_model)
    
    @staticmethod
    def name():
        return "Llama-2-7b".lower()


class MistralAI(LLMModel):
    def __init__(self, base_model_id = "llm/mistralai/Mistral-7B-v0.1", base_model_name = "Mistral-7B-v0.1", quantization = 'Q5_0', llm_loader = None, without_model = False, lora_path = None) -> None:
        super().__init__(base_model_id, base_model_name, quantization=quantization, llm_loader=llm_loader, without_model=without_model, lora_path = lora_path)
    
    @staticmethod
    def name():
        return "Mistral-7B-v0.1".lower()
    
class MistralAIInstruct(LLMModel):
    def __init__(self, base_model_id = "mistralai/Mistral-7B-Instruct-v0.2", base_model_name = "Mistral-7B-Instruct-v0.2", quantization = 'Q8_0', llm_loader = None, without_model = False, lora_path = None) -> None:
        super().__init__(base_model_id, base_model_name, quantization=quantization, llm_loader=llm_loader, without_model=without_model, lora_path = lora_path)
        self.tokenizer =  AutoTokenizer.from_pretrained("mistralai/Mistral-7B-Instruct-v0.2")
        if "finetune" in self.base_model_id :
            self.name = f"{self.base_model_name.lower()}-ft-discussion-2000-Q8_0"

    @staticmethod
    def name():
        return "Mistral-7B-Instruct-v0.2".lower() +"" if "finetune" in self.base_model_id else "ft-2000"
    
    def __call__(self, prompt, with_full_message = False) -> Any:
        prompt = self.construct_prompt_for_mistralAIInstruct(prompt)
            # print(prompt)
        return self.model(prompt, with_full_message)

    def construct_prompt_for_mistralAIInstruct(self, prompt):
        if '### USER' in prompt :
            prompt = self.from_mistral_to_mistralInstruct(prompt)
            prompt = prompt.replace("INPUT", "USER").replace("OUTPUT", "ASSISTANT")

        if isinstance(prompt, list):
            prompt = self.tokenizer.apply_chat_template(prompt, tokenize=False)

        return prompt

    def from_mistral_to_mistralInstruct(self, prompt):
        messages = [m for m in  prompt.split('### ') if m]
        output = []
        if messages[0].split()[0] == "SYSTEM" and messages[1].split()[0] == "USER": 
            messages[1] = "USER : " +messages[0].split("SYSTEM : ")[-1] + messages[1].split("USER : ")[-1]
            messages = messages[1:]

        prev_role, prev_content = "",""
        for mess in messages :
            role = mess.split()[0]
            content = mess.split(f"{role} : ")[-1]
            if prev_role != role and prev_role:
                output.append({'role' : prev_role.lower() , 'content' : prev_content})
                prev_content = content
            else : 
                prev_content += content
            prev_role = role
        output.append({'role' : prev_role.lower() , 'content' : prev_content})
        # print(output)
        return output
    
class NoLLM(LLMModel):
    def __init__(self, base_model_id = "None", base_model_name = "None", llm_loader = None, without_model = False) -> None:
        super().__init__(base_model_id, base_model_name, llm_loader=llm_loader, without_model=without_model)
    
    def __call__(self, prompt, stop = ["<end_output>", "\n\n\n"], with_full_message = False) -> Any:
        if with_full_message :
            return prompt, None
        return [('Japan', 'NORP')]
    
    def get_model(self, gguf_model_path = "", quantization = "", lora_path = ""):
        return None
    
    @staticmethod
    def name():
        return "None".lower()
    

