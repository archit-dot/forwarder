import asyncio, logging
from pathlib import Path
from aiogram import Bot, Dispatcher
from dotenv import load_dotenv
from app.config import load_config
from app.db import Database
from app.security import SecretBox
from app.services.forwarder import Manager
from app.controller import Controller

async def main():
    load_dotenv(); cfg=load_config(); Path('logs').mkdir(exist_ok=True)
    logging.basicConfig(level=getattr(logging,cfg.log_level,logging.INFO),format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',handlers=[logging.StreamHandler(),logging.FileHandler('logs/forwarder.log',encoding='utf-8')])
    db=Database(cfg.database_path); box=SecretBox(cfg.encryption_key); manager=Manager(cfg,db,box); controller=Controller(cfg,db,box,manager)
    bot=Bot(cfg.controller_bot_token); dp=Dispatcher(); dp.include_router(controller.router); logging.getLogger('main').info('Public multi-user controller starting'); await dp.start_polling(bot)
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
