from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 飞书应用凭证（在飞书开放平台 → 凭证与基础信息 里找）
    lark_app_id:     str = ""
    lark_app_secret: str = ""

    # SuperChat 服务地址
    superchat_url:   str = "http://localhost:8000"

    # SuperChat 网关 Bearer Token（用于 JWT/OAuth2 认证）
    superchat_access_token: str = ""

    # Agent 默认参数
    agent_id:        str = "cmg-bot"
    workspace_id:    str = "CMG"

    # 消息配置
    # 群聊中只响应 @机器人 的消息（True），或响应所有消息（False）
    group_at_only:   bool = True

    # 请求超时（秒）
    request_timeout: int = 50000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
