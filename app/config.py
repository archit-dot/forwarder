from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

def req(name: str) -> str:
    v=os.getenv(name,'').strip()
    if not v: raise RuntimeError(f'Missing required environment variable: {name}')
    return v

@dataclass(frozen=True)
class Config:
    controller_bot_token: str
    api_id: int
    api_hash: str
    owner_id: int
    encryption_key: bytes
    database_path: str
    log_level: str

def load_config():
    return Config(
        controller_bot_token=req('CONTROLLER_BOT_TOKEN'),
        api_id=int(req('API_ID')),
        api_hash=req('API_HASH'),
        owner_id=int(req('OWNER_ID')),
        encryption_key=req('ENCRYPTION_KEY').encode(),
        database_path=os.getenv('DATABASE_PATH','data/forwarder.db'),
        log_level=os.getenv('LOG_LEVEL','INFO').upper(),
    )
