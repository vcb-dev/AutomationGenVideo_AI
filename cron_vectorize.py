import os
import time
import uuid
import psycopg2
from dotenv import load_dotenv
from fastembed import TextEmbedding

load_dotenv('.env')

def sync_vectors():
    print("[Cron Vectorize] Khởi tạo mô hình Embedding (Local)...")
    t0 = time.time()
    # model_name defaults to BAAI/bge-small-en-v1.5, which is 133MB and very good for general text
    # We can use a multilingual one if needed, but for names, English is fine.
    # BAAI/bge-small-en-v1.5 produces 384-dimensional vectors!
    # Wait, in Supabase, I created `vector(1536)` because I assumed OpenAI!
    # I need to CHANGE the Supabase table to `vector(384)`!
    pass
