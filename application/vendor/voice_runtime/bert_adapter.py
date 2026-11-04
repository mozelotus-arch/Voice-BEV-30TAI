"""Model-bound BERT embedding/head adapter; no business actions or SDK import."""
import json,threading,time
from pathlib import Path

import numpy as np


def softmax(values):
    scaled=np.exp(values-values.max(axis=-1,keepdims=True))
    return scaled/scaled.sum(axis=-1,keepdims=True)


class BertAdapter:
    @classmethod
    def from_bundle(cls,graph,bundle,tokenizer=None):
        if bundle.metadata.get('adapter')!='bert-single-sequence-split-v1':
            raise ValueError('Unsupported BERT adapter family')
        if graph.spec!=bundle.spec:
            raise ValueError('Graph and BERT model package differ')
        files={name:bundle.role(name) for name in
               ('word_embeddings','position_embeddings','token_type_embeddings')}
        return cls(graph,bundle.folder,bundle.role('interface'),tokenizer,embedding_files=files)

    def __init__(self,graph,embedding_folder,interface_file,tokenizer=None,*,embedding_files=None):
        self.graph=graph;self.lock=threading.RLock()
        folder=Path(embedding_folder)
        self.interface=json.loads(Path(interface_file).read_text(encoding='utf-8'))
        self.length=self.interface['sequence_length']
        if (type(self.interface.get('batch_size')) is not int or self.interface['batch_size']!=1 or
            type(self.length) is not int or self.length<1):
            raise ValueError('This adapter requires a static single-sequence BERT graph')
        def labels(mapping):
            if (not isinstance(mapping,dict) or not mapping or
                any(not isinstance(name,str) or not name or type(index) is not int for name,index in mapping.items()) or
                set(mapping.values())!=set(range(len(mapping)))):
                raise ValueError('Head label IDs must be contiguous from zero')
            return {index:name for name,index in mapping.items()}
        self.intents=labels(self.interface['intent_to_id'])
        self.slots=labels(self.interface['bio_to_id'])
        files=embedding_files or {name:folder/(name+'.npy') for name in
                                 ('word_embeddings','position_embeddings','token_type_embeddings')}
        self.word=np.load(str(files['word_embeddings']),mmap_mode='r')
        self.position=np.load(str(files['position_embeddings']),mmap_mode='r')
        self.types=np.load(str(files['token_type_embeddings']),mmap_mode='r')
        if any(a.ndim!=2 or a.dtype!=np.float32 for a in (self.word,self.position,self.types)):
            raise ValueError('Embedding tables must be FP32 matrices')
        self.hidden=self.word.shape[1]
        if self.hidden<1 or len(self.word)<1 or self.position.shape[1]!=self.hidden or self.types.shape[1]!=self.hidden or self.position.shape[0]<self.length or len(self.types)<1:
            raise ValueError('Embedding tables do not match the compiled sequence')
        self.shapes=[(1,self.length,self.hidden,1),(1,self.length,self.length,1)]
        if [c.shape for c in graph.contracts]!=self.shapes or any(c.dtype!='float32' for c in graph.contracts):
            raise ValueError('Compiled input contract differs from model package')
        expected={'intent_logits':[1,len(self.intents)],'slot_logits':[1,self.length,len(self.slots)]}
        if self.interface.get('outputs')!=expected:
            raise ValueError('Label map and declared output dimensions differ')
        self.buffers=[np.empty(s,np.float32) for s in self.shapes]
        self.tokenizer=tokenizer
        if tokenizer is not None:
            tokenizer.no_truncation();tokenizer.no_padding()
            if tokenizer.token_to_id('[PAD]')!=0:
                raise ValueError('This adapter family requires PAD token ID zero')

    def _prepare(self,ids,mask):
        expected=(1,self.length)
        if (not isinstance(ids,np.ndarray) or not isinstance(mask,np.ndarray) or ids.shape!=expected or mask.shape!=expected or
            ids.dtype!=np.int64 or mask.dtype!=np.int64):
            raise ValueError('Require INT64 [1,sequence_length] IDs and attention mask')
        if np.any(ids<0) or np.any(ids>=len(self.word)) or np.any((mask!=0)&(mask!=1)):
            raise ValueError('Token ID or mask outside model contract')
        embedding=self.buffers[0].reshape(self.length,self.hidden)
        # IDs are checked above. clip avoids np.take's extra raise-mode buffer;
        # it cannot silently clip an out-of-range user input.
        np.take(self.word,ids[0],axis=0,out=embedding,mode='clip')
        np.add(embedding,self.types[0],out=embedding)
        np.add(embedding,self.position[:self.length],out=embedding)
        bias=self.buffers[1].reshape(self.length,self.length)
        bias[:]=np.where(mask[0]>0,np.float32(0),np.float32(-10000))[None,:]
        return self.buffers

    def predict_ids(self,ids,mask):
        requested=time.perf_counter()
        with self.lock:
            acquired=time.perf_counter()
            arrays=self._prepare(ids,mask);prepared=time.perf_counter()
            def consume(outputs):
                shapes=[(1,len(self.intents)),(1,self.length,len(self.slots))]
                if len(outputs)!=2:raise ValueError('Expected intent and token head outputs')
                result=[]
                for output,shape in zip(outputs,shapes):
                    if output.size!=int(np.prod(shape)) or not np.issubdtype(output.dtype,np.floating) or not np.isfinite(output).all():
                        raise ValueError('Head dimensions/values differ from model interface')
                    result.append(output.reshape(shape).copy())
                return result
            logits=self.graph.forward(arrays,reducer=consume)
            completed=time.perf_counter()
            return logits,dict(queue_seconds=acquired-requested,cpu_adapter_seconds=prepared-acquired,
                graph_call_seconds=completed-prepared,api_seconds=completed-requested,
                scope='BERT IDs API including lock wait; excludes model load/tokenization/business policy')

    def interpret(self,text):
        if self.tokenizer is None:raise RuntimeError('Text interpretation requires the model tokenizer')
        if not isinstance(text,str):raise TypeError('Text must be a string')
        requested=time.perf_counter()
        with self.lock:
            encoded=self.tokenizer.encode(text)
            if len(encoded.ids)>self.length:raise ValueError('Text exceeds compiled BERT sequence; no silent truncation')
            if len(encoded.offsets)!=len(encoded.ids) or len(encoded.type_ids)!=len(encoded.ids):
                raise ValueError('Tokenizer IDs, offsets and type IDs differ in length')
            if any(not 0<=begin<=end<=len(text) for begin,end in encoded.offsets):
                raise ValueError('Tokenizer offsets are outside the input text')
            if any(encoded.type_ids):raise ValueError('This source model uses single-sequence token type zero')
            ids=np.asarray([encoded.ids+[0]*(self.length-len(encoded.ids))],dtype=np.int64)
            mask=np.asarray([[1]*len(encoded.ids)+[0]*(self.length-len(encoded.ids))],dtype=np.int64)
            logits,timing=self.predict_ids(ids,mask)
            intent,slots=[softmax(head) for head in logits]
            intent_index=int(intent[0].argmax())
            predictions=[dict(start=int(begin),end=int(end),label=self.slots[int(slots[0,i].argmax())],
                confidence=float(slots[0,i].max())) for i,(begin,end) in enumerate(encoded.offsets) if begin!=end]
            return dict(text=text,intent=self.intents[intent_index],intent_confidence=float(intent[0,intent_index]),
                token_predictions=predictions,offsets=encoded.offsets,slot_ids=slots[0].argmax(-1).tolist(),
                slot_confidences=slots[0].max(-1).tolist(),processing_seconds=time.perf_counter()-requested,
                timing=timing,model=self.graph.spec.name,business_action_executed=False)
