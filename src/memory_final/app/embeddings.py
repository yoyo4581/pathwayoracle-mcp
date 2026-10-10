from transformers import AutoTokenizer, AutoModel
import torch
from langchain_core.embeddings import Embeddings
from typing import List

class MemEmbeddings(Embeddings):
    def __init__(self):
        self.tokenizer = AutoTokenizer.from_pretrained("dmis-lab/biobert-base-cased-v1.2")
        self.model = AutoModel.from_pretrained("dmis-lab/biobert-base-cased-v1.2")
        # Optional: move model to GPU if available
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.model.eval()  # Set to evaluation mode

    def embed_query(self, text: str) -> List[float]:
        return self.get_embeddings([text])[0]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self.get_embeddings(texts)

    def get_embeddings(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """Generate embeddings with batching for large text lists."""
        all_embeddings = []
        
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]
            
            inputs = self.tokenizer(
                batch_texts,
                return_tensors="pt",
                truncation=True,
                padding=True,
                max_length=512
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            
            with torch.no_grad():
                outputs = self.model(**inputs)
            
            embeddings = outputs.last_hidden_state.mean(dim=1)
            all_embeddings.extend(embeddings.cpu().numpy().tolist())
        
        return all_embeddings

