from langchain_openai import OpenAIEmbeddings
from tools.config import LLM_CONFIG

# 阿里 MaaS OpenAI Compatible 接口兼容配置
# 避免 LangChain 内部 tokenizer 改写 input 格式
# 所以加了这个参数check_embedding_ctx_length=False,
emb = OpenAIEmbeddings(
    api_key=LLM_CONFIG["api_key"],
    base_url=LLM_CONFIG["base_url"],
    model=LLM_CONFIG["embedding_model"],
    check_embedding_ctx_length=False,
    chunk_size=10,
)

print(emb.embed_query("你好"))


# from openai import OpenAI
# from config import LLM_CONFIG
#
# client = OpenAI(
#     api_key=LLM_CONFIG["api_key"],
#     base_url=LLM_CONFIG["base_url"],
# )
#
# resp = client.embeddings.create(
#     model=LLM_CONFIG["embedding_model"],
#     input=[
#         "客户维度表",
#         "产品维度表",
#         "销售订单表"
#     ]
# )
#
# print(len(resp.data))
