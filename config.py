from pydantic_settings import BaseSettings
import json


class Settings(BaseSettings):
    # SGLang
    sglang_base_url: str = "http://cmgai.video.cloud.cctv.com/gateway/v1"
    sglang_model: str = "Qwen"
    # 兼容 OpenAI/ModelScope 网关鉴权
    sglang_api_key: str = ""
    modelscope_api_token: str = ""
    sglang_headers: str = '{"Content-Type": "application/json", "X-TC-Project": "50", ' \
                          '"Host": "cmgai.video.cloud.cctv.com", "X-TC-Action": "/v1/chat/completions", ' \
                          '"X-TC-Version": "2020-10-01", "X-TC-Service": "qwen-122b"}'

    @property
    def sglang_headers_dict(self) -> dict:
        try:
            return json.loads(self.sglang_headers)
        except Exception as e:
            print(e)
            return {}

    @property
    def effective_api_key(self) -> str:
        """
        优先使用 SGLANG_API_KEY，未设置时回落到 MODELSCOPE_API_TOKEN。
        为空则返回 EMPTY（兼容无需鉴权的本地网关）。
        """
        return (self.sglang_api_key or self.modelscope_api_token or "EMPTY").strip()

    # Skills
    skills_dir: str = "./skills"

    # SQLite
    db_path: str = "./data/openclaw.db"
    skill_memory_db: str = ".data/openclaw.db"
    # Agent
    max_tool_rounds: int = 15

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
