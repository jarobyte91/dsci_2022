import pandas as pd
import matplotlib
import os
# os.environ['TRANSFORMERS_OFFLINE'] = '1' # Indicating transformers for offline mode
from functools import partial
from pathlib import Path
from tokenizers import Tokenizer
from transformers import AutoTokenizer, AutoModel
import transformers
from transformers.models.auto.tokenization_auto import logger
import random
import torch
import torchmetrics
import torch.nn as nn
from torch.utils import data
from torch.utils.data import Dataset, DataLoader, random_split
import pytorch_lightning as pl

class CHUNKSUMM(pl.LightningModule):
    def __init__(self, model, learning_rate=6e-5, n_classes=2, enable_chunk=False):
        super().__init__()
        self.bert = model
        # Freezing bert params
        # for param in self.bert.parameters():
        #     param.requires_grad = False
        # self.bert.eval()
        self.criterion = nn.BCEWithLogitsLoss()
        self.l1 = torch.nn.Linear(768, n_classes)
        self.learning_rate = learning_rate
        self.accuracy = torchmetrics.Accuracy()
        self.auc = torchmetrics.AUROC(num_classes=n_classes)
        self.enable_chunk = enable_chunk
        # self.save_hyperparameters() # Saves every *args in _init_() in checkpoint file. # Slows trainer.predict
        self.save_hyperparameters(ignore=["bert"])

    def forward(
        self, 
        input_ids, 
        attention_mask, 
        token_type_ids, 
        train=False
    ):
        """Can handle more than 512 tokens"""
        embed2d = self.get_embedding(
            input_ids, 
            attention_mask, 
            token_type_ids
        )
        logits = self.l1(embed2d)  
        if train:
            return logits
        else: 
            return torch.softmax(logits,dim=-1)

    def get_embedding(self, input_ids, attention_mask, token_type_ids):
        if self.enable_chunk:
            batch_chunks = [
                self.chunk(batch) for batch in (input_ids, attention_mask, token_type_ids)
            ]
            handler = []
            for chunk in zip(
                batch_chunks[0], 
                batch_chunks[1], 
                batch_chunks[2]
            ):
                chunk_hidden_states = self.bert(
                    chunk[0], 
                    chunk[1], 
                    chunk[2], 
                    output_hidden_states=True
                )[2]
                chunk_embed2d = torch.stack(chunk_hidden_states)[-5:].mean(0)
                handler.append(chunk_embed2d)
            contextual_encoding = torch.cat(handler, dim=1)
            embed2d = contextual_encoding
        else:
            hidden_states = self.bert(
                input_ids[:, :512], 
                attention_mask[:, :512], 
                token_type_ids[:, :512], 
                output_hidden_states=True
            )[2]
            mean_hidden_states = torch.stack(hidden_states)[-5:].mean(0)
            contextual_encoding = mean_hidden_states
            embed2d = contextual_encoding

        return embed2d

    def training_step(self, batch, batch_ids=None):
        outputs = self(
            batch["input_ids"], 
            batch["attention_mask"], 
            batch["token_type_ids"],
            train=True
        )
        labels = self.expand_targets(batch["targets"].float()) 
        labels = labels.reshape_as(outputs)
        loss = self.criterion(outputs, labels) 
        #acc = self.accuracy(outputs, labels.int())
        auc = self.auc(outputs, labels.int())       
        self.log("Loss_train", loss, prog_bar=True, logger=True)
        self.log("Auc_train", auc, prog_bar=True, logger=True)
        return {"loss": loss, "predictions": outputs, "labels": labels}

    def validation_step(self, batch, batch_idx):
        outputs = self(
            batch["input_ids"], batch["attention_mask"], batch["token_type_ids"])
        labels = self.expand_targets(batch["targets"].float()) 
        labels = labels.reshape_as(outputs)
        loss = self.criterion(outputs,labels)   
        auc = self.auc(outputs, labels.int())
        self.log("Loss_val", loss, prog_bar=True, logger=True)
        self.log("Auc_val", auc, prog_bar=True, logger=True)
        return loss

    def test_step(self, batch, batch_idx):
        outputs = self(batch["input_ids"], batch["attention_mask"], batch["token_type_ids"])
        labels = self.expand_targets(batch["targets"].float()) 
        labels = labels.reshape_as(outputs)
        loss = self.criterion(outputs, labels)
        #acc = self.accuracy(outputs, labels.int())
        auc = self.auc(outputs, labels.int())
        self.log("Test_loss", loss, prog_bar=True, logger=True)
        self.log("Test_auc", auc, prog_bar=True, logger=True)
        return loss

    def predict_step(self, batch, batch_ids, dataloader_idx=None):
        outputs = self(
            batch["input_ids"], 
            batch["attention_mask"], 
            batch["token_type_ids"]
        )
        return outputs

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.learning_rate)

    @property
    def chunk(self):
        return partial(torch.split, split_size_or_sections=512, dim=1)

    def expand_targets(self,targets):
        """This returns the Two dimentional targets given the single correct label"""
        # 0 -> [0,1]  , 1 -> [1,0] =  (IN,OUT) class.
        out = torch.stack(
            [
                torch.tensor([1.,0.]) if val else torch.tensor([0.,1.])  
                for batch in  targets.bool() for val in batch
            ]
        )
        return out.to(self.device)
    
    
def get_token_scores(model, tokenizer, text:[str]):
    tokenized_input = tokenizer(text, return_tensors = 'pt')
    model.eval()
    return tokenized_input['input_ids'], model(**tokenized_input) # (IN,OUT) probabilities


class SuMM_with_tokenizer(Dataset):
    """This will create a map-style database to be used by DataLoader,
    1. Uses pretrained tokenizer to get tokens from text"""
    def __init__(
        self,
        data: pd.DataFrame,
        tokenizer, 
        process_paper_level = False
    ):
        super().__init__()
        self.data = data
        self.tokenizer = tokenizer
        self.process_paper_level =  process_paper_level
        self.papers_dict = {i:v for i, v in enumerate(data.groupby('paper_id').groups.values())}

    def __len__(self):
        if self.process_paper_level:
            # Number of total papers
            out = self.data.paper_id.value_counts().shape[0]
            return out
        # Number of total sentences in papers.
        return self.data.shape[0] 

    def __getitem__(self, index):
        if self.process_paper_level:
            paper = self.data.loc[self.papers_dict[index], :]
            inputs = self.combine_inputs(paper, self.tokenizer)
            return {
                # Flattening input is important during auto-collation by DataLoader
                "input_ids": inputs["input_ids"].flatten(),
                "attention_mask": inputs["attention_mask"].flatten(),
                "token_type_ids": inputs["token_type_ids"].flatten(),
                "targets": inputs["targets"].flatten(),
            }
        else:
            datum = self.data.iloc[index,:]
            paper_id = datum.paper_id
            text = datum.text
            inputs = self.tokenizer(
                text,
                add_special_tokens = True,
                return_token_type_ids = True,
                return_attention_mask = True,
                return_tensors="pt",
            )
            input_len = len(inputs['input_ids'].flatten())
            target = [1.] * input_len if datum.in_summary else [0.] * input_len
            target = torch.tensor(target).float() 
        return {
            # Flattening input is important during auto-collation by DataLoader
            "input_ids": inputs["input_ids"].flatten(),
            "attention_mask": inputs["attention_mask"].flatten(),
            "token_type_ids": inputs["token_type_ids"].flatten(),
            "targets": target,
        }

    def combine_inputs(
        self,
        data:pd.DataFrame,tokenizer
    ):
        out = data\
        .assign(
            tokens = lambda df: df.text.map(
                lambda y: tokenizer(y, add_special_tokens=False)
            )
        )\
        .assign(
            propagated_labels = lambda df: df.apply(
                lambda row: row["tokens"].update(
                    {"targets":[row["in_summary"]] * len(row["tokens"]["input_ids"])}
                ),
                axis='columns'
            )
        )\
        .groupby("paper_id").tokens\
        .agg(
            lambda l: {
                "input_ids":torch.tensor(sum([d["input_ids"] for d in l], start = [])),
                "token_type_ids":torch.tensor(sum([d["token_type_ids"] for d in l], start = [])),
                "attention_mask":torch.tensor(sum([d["attention_mask"] for d in l], start = [])),
                "targets":torch.tensor(sum([d["targets"] for d in l], start = [])),
            }
        )

        return out.to_list()[0]

class SummDataModule(pl.LightningDataModule):
    """This is wrapper class around the Dataset class to return Dataloader objects"""
    def __init__(
        self,
        trainData:pd.DataFrame,
        testData:pd.DataFrame,
        valData:pd.DataFrame,
        SuMMDataset: Dataset,
        tokenizer,
        batch_size = 8,
        workers = 4,
        train_size = "full",
        process_paper_level = False
    ):
        super().__init__()
        self.batch_size = batch_size
        self.trainData = trainData
        self.testData = testData
        self.tokenizer = tokenizer
        self.valData= valData
        self.workers = workers
        self.batch_size= batch_size
        self.SuMMDataset = SuMMDataset
        self.train_size = train_size
        self.process_paper_level = process_paper_level

    def setup(self, stage=None):
        """# make assignments here (val/train/test split)
        # called on every process in DDP"""

        if self.train_size == "full":
            # Dataset is balanced
            self.train_dataset = self.SuMMDataset(
                data = self.trainData,
                tokenizer = self.tokenizer,
                process_paper_level = self.process_paper_level
            )
        else:
            ## Change the code to deal with samples of papers and not sentences
            self.train_dataset = self.SuMMDataset(
                data = self.trainData.sample(self.train_size),
                tokenizer = self.tokenizer,
                process_paper_level = self.process_paper_level
            )
        # The validation size is kept fixed regardless of training size.
        self.val_dataset = self.SuMMDataset(
            data = self.valData,
            tokenizer = self.tokenizer,
            process_paper_level = self.process_paper_level
        )
        self.test_dataset = self.SuMMDataset(
            data = self.testData,
            tokenizer = self.tokenizer,
            process_paper_level = self.process_paper_level
        )

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            shuffle=False,
            collate_fn=self.collate,
            num_workers=self.workers,
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset,
            batch_size=self.batch_size,
            collate_fn=self.collate,
            num_workers=self.workers,
        )

    def test_dataloader(self):
        return DataLoader(
            self.test_dataset,
            batch_size=self.batch_size,
            collate_fn=self.collate,
            num_workers=self.workers,
        )

    @staticmethod
    def collate(batch):
        """The inputs are padded to the maximum token len in a batch including rationale_mask"""
        input_ids_batch, attention_mask_batch, targets_batch = [], [], []
        maximum = max(len(item["input_ids"]) for item in batch)
        
        def pad(tensor, max=maximum):
            out = torch.zeros(max)
            out[: len(tensor)] = tensor
            return out

        # list of dict with tensor values
        for datum in batch:
            input_ids, attention_mask,targets = (
                datum["input_ids"],
                datum["attention_mask"],
                datum["targets"]
            )
            input_ids_batch.append(pad(input_ids))  # '0' is padding token idx in embedding layer
            attention_mask_batch.append(pad(attention_mask))  # '0' is padding value in attention mask
            targets_batch.append(pad(targets)) # '0' is for padding value for targets.
        out = {
            "input_ids": torch.stack(input_ids_batch).int(),
            "attention_mask": torch.stack(attention_mask_batch).int(),
            "token_type_ids": torch.zeros(len(batch), maximum).int(),
            "targets": torch.stack(targets_batch),
        }
        return out

    @property
    def train_len(self):
        return len(self.train_dataset.data)

    @property
    def test_len(self):
        return len(self.test_dataset.data)

    @property
    def val_len(self):
        return len(self.val_dataset.data)

