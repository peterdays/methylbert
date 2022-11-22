from torch.utils.data import Dataset
import torch

import numpy as np
from copy import deepcopy
import multiprocessing as mp
from functools import partial
import random

from methylbert.data.vocab import WordVocab

def _line2tokens(l, tokenizer, max_len=120):
	'''
			convert a text line into a list of tokens converted by tokenizer 
			
	'''

	l = l.strip().split(" ")

	tokened = [tokenizer.to_seq(b) for b in l]
	if len(tokened) > max_len:
		return tokened[:max_len]
	else:
		return tokened + [[tokenizer.pad_index] for k in range(max_len-len(tokened))]


def _line2tokens_finetune(l, tokenizer, max_len=120, headers=None):
	# Separate n-mers tokens and labels from each line 
	l = l.strip().split("\t")

	if ( not headers ) and ( len(l) == 22 ):
		headers = ["seqname", "flag", "ref_name", "ref_pos", "map_quality", "cigar", "next_ref_name", "next_ref_pos", "length", "seq", "qual", "MD", "PG", "XG", "NM", "XM", "XR", "dna_seq", "methyl_seq", "dmr_ctype", "dmr_label", "ctype"]
	elif ( not headers ) and ( len(l) == 5 ):
		headers = ["dna_seq", "methyl_seq", "dmr_ctype", "ctype", "dmr_label"]
	elif ( not headers ) and ( len(l) == 7 ):
		headers = ["dna_seq", "methyl_seq", "dmr_ctype", "ctype", "dmr_label",  "seq", "xm_tag"]
	
	if len(headers) == len(l):
		l = {k: v for k, v in zip(headers, l)}

	l["dna_seq"] = l["dna_seq"].split(" ")
	
	# Tokenisation
	l["dna_seq"] = [tokenizer.to_seq(b) for b in l["dna_seq"]]
	l["methyl_seq"] = [int(m) for m in l["methyl_seq"]]
	l["ctype_label"] = int(l["ctype"] == l["dmr_ctype"])
	l["dmr_label"] = int(l["dmr_label"])

	if len(l["dna_seq"]) > max_len:
		l["dna_seq"] = l["dna_seq"][:max_len]
		l["methyl_seq"] = l["methyl_seq"][:max_len]
	else:
		cur_seq_len=len(l["dna_seq"])
		l["dna_seq"] = l["dna_seq"]+[[tokenizer.pad_index] for k in range(max_len-cur_seq_len)]
		l["methyl_seq"] = l["methyl_seq"] + [2 for k in range(max_len-cur_seq_len)]

	return l

class MethylBertDataset(Dataset):
	def __init__(self):
		pass
			
	def __len__(self):
		return self.lines.shape[0] if type(self.lines) == np.array else len(self.lines)


class MethylBertPretrainDataset(MethylBertDataset):
	def __init__(self, f_path: str, vocab: WordVocab, seq_len: int, random_len=False, n_cores=50):

		self.vocab = vocab
		self.seq_len = seq_len
		self.f_path = f_path
		self.random_len = random_len

		# Define a range of tokens to mask based on k-mers 
		self.mask_list = self._get_mask()

		# Read all text files and convert the raw sequence into tokens
		with open(self.f_path, "r") as f_input:
			print("Open data : %s"%f_input)
			raw_seqs = f_input.read().splitlines()

		print("Total number of sequences : ", len(raw_seqs))

		# Multiprocessing for the sequence tokenisation
		with mp.Pool(n_cores) as pool:
			line_labels = pool.map(partial(_line2tokens, tokenizer=self.vocab, max_len=self.seq_len), raw_seqs)
			del raw_seqs
			print("Lines are processed")
			self.lines = torch.squeeze(torch.tensor(np.array(line_labels, dtype=np.int16)))
		del line_labels

	def __getitem__(self, index): 

		dna_seq = self.lines[index].clone()

		# Random len
		if self.random_len and index % 2 == 0:
			dna_seq = dna_seq[:random.randint(5, self.seq_len)] 
		
		# Padding
		if dna_seq.shape[0] < self.seq_len:
			pad_num = self.seq_len+2-dna_seq.shape[0]
			dna_seq = torch.cat((dna_seq, 
								torch.tensor([self.vocab.pad_index for i in range(pad_num)])))
		# Mask 
		masked_dna_seq, dna_seq, bert_mask = self._masking(dna_seq)
		
		return {"input": masked_dna_seq,
				"label": dna_seq,
				"mask" : bert_mask}
	
	def subset_data(self, n_seq: int):
		self.lines = random.sample(self.lines, n_seq)

	def _get_mask(self):
		'''
			Relative positions from the centre of masked region 
			e.g) [-1, 0, 1] for 3-mers 
		'''
		half_length = int(self.vocab.kmers/2)
		mask_list = [-1*half_length + i for i in range(half_length)] + [i for i in range(1, half_length+1)]
		if self.vocab.kmers % 2 == 0:
			mask_list = mask_list[:-1]

		return mask_list

	def _masking(self, inputs: torch.Tensor, threshold=0.15):
		""" 
			Moidfied version of masking token function
			Originally developed by Huggingface (datacollator) and DNABERT
			
			https://github.com/huggingface/transformers/blob/9a24b97b7f304fa1ceaaeba031241293921b69d3/src/transformers/data/data_collator.py#L747

			https://github.com/jerryji1993/DNABERT/blob/bed72fc0694a7b04f7e980dc9ce986e2bb785090/examples/run_pretrain.py#L251

			Added additional tasks to handle each sequence
			Lines using tokenizer were modified due to different tokenizer object structure

		"""

		labels = inputs.clone()

		# Sample tokens with given probability threshold
		probability_matrix = torch.full(labels.shape, threshold) # tensor filled with 0.15

		# Handle special tokens and padding
		special_tokens_mask = [
			val < 5 for val in labels.tolist()
		]
		probability_matrix.masked_fill_(torch.tensor(special_tokens_mask, dtype=torch.bool), value=0.0)
		padding_mask = labels.eq(self.vocab.pad_index)
		probability_matrix.masked_fill_(padding_mask, value=0.0)

		masked_indices = torch.bernoulli(probability_matrix).bool() # get bernoulli for non-special token masks

		# change masked indices
		masked_index = deepcopy(masked_indices)
		
		# This function handles each sequence
		end = torch.where(probability_matrix!=0)[0].tolist()[-1] # end of the sequence
		mask_centers = set(torch.where(masked_index==1)[0].tolist()) # mask locations

		new_centers = deepcopy(mask_centers)
		for center in mask_centers:
			for mask_number in self._mask_list:# add neighbour loci 
				current_index = center + mask_number 
				if current_index <= end and current_index >= 1:
					new_centers.add(current_index)

		new_centers = list(new_centers)
		masked_indices[new_centers] = True
		
		# Avoid loss calculation on unmasked tokens
		labels[~masked_indices] = -100 

		# 80% of the time, we replace masked input tokens with tokenizer.mask_token ([MASK])
		indices_replaced = torch.bernoulli(torch.full(labels.shape, 0.8)).bool() & masked_indices
		inputs[indices_replaced] = self.vocab.mask_index

		# 10% of the time, we replace masked input tokens with random word
		indices_random = torch.bernoulli(torch.full(labels.shape, 0.5)).bool() & masked_indices & ~indices_replaced
		random_words = torch.randint(len(self.vocab), labels.shape, dtype=torch.int32)
		inputs[indices_random] = random_words[indices_random]

		# The rest of the time (10% of the time) we keep the masked input tokens unchanged

		# Special tokens (SOS, EOS)
		if end < inputs.shape[0]:
			inputs[end] = self.vocab.eos_index
		else:
			inputs[-1] = self.vocab.eos_index

		labels = torch.cat((torch.tensor([-100]), labels))
		inputs = torch.cat((torch.tensor([self.vocab.sos_index]), inputs))
		masked_index = torch.cat((torch.tensor([False]), masked_index))


		return inputs, labels, masked_index

class MethylBertFinetuneDataset(MethylBertDataset):
	def __init__(self, f_path: str, vocab: WordVocab, seq_len: int, n_cores=10):

		self.vocab = vocab
		self.seq_len = seq_len
		self.f_path = f_path

		# Read all text files and convert the raw sequence into tokens
		with open(self.f_path, "r") as f_input:
			raw_seqs = f_input.read().splitlines()

		if "XM" in raw_seqs[0]:
			headers = raw_seqs[0].split("\t")
			raw_seqs = raw_seqs[1:]
		else:
			headers=None
		print("Total number of sequences : ", len(raw_seqs))

		# Multiprocessing for the sequence tokenisation
		with mp.Pool(n_cores) as pool:
			self.lines = pool.map(partial(_line2tokens_finetune, tokenizer=self.vocab, max_len=self.seq_len, headers=headers), raw_seqs)
			del raw_seqs

		self.ctype_label_count = self._get_cls_num()
		print("# of reads in each label: ", self.ctype_label_count)
		
		
	def _get_cls_num(self):
		# unique labels
		ctype_labels=[l["ctype_label"] for l in self.lines]
		labels = list(set(ctype_labels))
		label_count = np.zeros(len(labels))
		for l in labels:
			label_count[l] = sum(np.array(ctype_labels) == l)
		return label_count

	def num_dmrs(self):
		return len(set([l["dmr_label"] for l in self.lines]))
	
	def subset_data(self, n_seq):
		self.lines = self.lines[:n_seq]

	def __getitem__(self, index): 

		item = deepcopy(self.lines[index])
		
		item["dna_seq"] = torch.squeeze(torch.tensor(np.array(item["dna_seq"], dtype=np.int32)))
		item["methyl_seq"] = torch.squeeze(torch.tensor(np.array(item["methyl_seq"], dtype=np.int8)))
		item["is_cpg"] = item["methyl_seq"] < 2
	
		# Special tokens (SOS, EOS)

		end = torch.where(item["dna_seq"]!=self.vocab.pad_index)[0].tolist()[-1] + 1 # end of the read
		if end < item["dna_seq"].shape[0]:
			item["dna_seq"][end] = self.vocab.eos_index
			item["methyl_seq"][end] = 2
			item["is_cpg"][end] = 0
		else:
			item["dna_seq"][-1] = self.vocab.eos_index
			item["methyl_seq"][-1] = 2
			item["is_cpg"][-1] = 0
		
		item["dna_seq"] = torch.cat((torch.tensor([self.vocab.sos_index]), item["dna_seq"]))
		item["methyl_seq"] = torch.cat((torch.tensor([2]), item["methyl_seq"]))
		item["is_cpg"] = torch.cat((torch.tensor([0]), item["is_cpg"])).to(torch.long)
		return item
	
