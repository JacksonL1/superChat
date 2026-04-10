from pydantic_settings import BaseSettings
import json


class Settings(BaseSettings):
    # SGLang
    sglang_base_url: str = "http://localhost:8000/v1"
    sglang_model: str = "default"
    # 兼容 OpenAI/ModelScope 网关鉴权
    sglang_api_key: str = ""
    modelscope_api_token: str = ""
    sglang_headers: str = '{"Content-Type": "application/json"}'

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


    # Gateway 认证（JWT / OAuth2 Bearer）
    # 生产环境建议保持开启，禁止基于本地来源自动信任。
    auth_enabled: bool = False
    auth_jwt_secret: str = "CHANGE_ME_IN_PROD"
    auth_jwt_algorithm: str = "HS256"
    auth_issuer: str = ""
    auth_audience: str = ""
    auth_required_scopes: str = "gateway:chat"
    auth_clock_skew_seconds: int = 30

    # 服务端自身调用 Gateway 时使用的 Bearer Token（CLI / 内部集成）
    gateway_access_token: str = ""

    # 向量记忆
    embedding_enabled: bool = True
    embedding_model: str = "text-embedding-3-small"
    embedding_similarity_threshold: float = 0.75
    embedding_max_memories: int = 5
    embedding_max_chars: int = 2000

    # Skills
    skills_dir: str = "./skills"

    # SQLite
    db_path: str = "./data/superChat.db"
    skill_memory_db: str = ".data/superChat.db"
    # Agent
    max_tool_rounds: int = 15

    # Bash 工具安全策略
    # 逗号分隔的命令白名单，仅允许这些命令作为入口执行
    bash_allowed_commands: str = (
        "python,python3,pip,pip3,uv,pytest,"
        "ls,pwd,cat,head,tail,sed,awk,rg,find,echo,"
        "git,cp,mv,mkdir,touch, curl, wget"
    )
    # 逗号分隔的危险片段黑名单（命中即拒绝）
    bash_blocked_patterns: str = (
        "rm -rf,shutdown,reboot,poweroff,:(){,mkfs,dd if=,/etc/passwd,"
        "chmod 777,> /dev/sda"
    )
    # 是否允许 shell 操作符（如 &&、|、>、; 等）。默认关闭。
    bash_allow_shell_operators: bool = True
    # bash 工具执行目录根（相对项目根）
    bash_workspace_root: str = "."
    bash_max_args: int = 64

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
