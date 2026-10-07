import sqlite3
from pathlib import Path
from contextlib import contextmanager

class Database:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True); self.path=path; self.init()
    @contextmanager
    def conn(self):
        c=sqlite3.connect(self.path); c.row_factory=sqlite3.Row
        try: yield c; c.commit()
        finally: c.close()
    def init(self):
        with self.conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, connected INTEGER DEFAULT 0, running INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, encrypted_session BLOB NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS sources(user_id INTEGER NOT NULL, ref TEXT NOT NULL, UNIQUE(user_id,ref));
            CREATE TABLE IF NOT EXISTS destinations(user_id INTEGER NOT NULL, ref TEXT NOT NULL, UNIQUE(user_id,ref));
            CREATE TABLE IF NOT EXISTS settings(user_id INTEGER PRIMARY KEY, skip_links INTEGER DEFAULT 0);
            ''')
    def ensure_user(self,uid,username='',first_name=''):
        with self.conn() as c:
            c.execute('INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)',(uid,username,first_name))
            c.execute('UPDATE users SET username=?,first_name=? WHERE user_id=?',(username,first_name,uid))
            c.execute('INSERT OR IGNORE INTO settings(user_id) VALUES(?)',(uid,))
    def set_session(self,uid,data):
        with self.conn() as c:
            c.execute('INSERT OR REPLACE INTO sessions(user_id,encrypted_session) VALUES(?,?)',(uid,data)); c.execute('UPDATE users SET connected=1 WHERE user_id=?',(uid,))
    def get_session(self,uid):
        with self.conn() as c:
            r=c.execute('SELECT encrypted_session FROM sessions WHERE user_id=?',(uid,)).fetchone(); return r['encrypted_session'] if r else None
    def disconnect(self,uid):
        with self.conn() as c:
            c.execute('DELETE FROM sessions WHERE user_id=?',(uid,)); c.execute('UPDATE users SET connected=0,running=0 WHERE user_id=?',(uid,))
    def refs(self,table,uid):
        with self.conn() as c: return [r['ref'] for r in c.execute(f'SELECT ref FROM {table} WHERE user_id=? ORDER BY rowid',(uid,))]
    def add_ref(self,table,uid,ref):
        with self.conn() as c: c.execute(f'INSERT OR IGNORE INTO {table}(user_id,ref) VALUES(?,?)',(uid,ref))
    def remove_ref(self,table,uid,ref):
        with self.conn() as c: c.execute(f'DELETE FROM {table} WHERE user_id=? AND ref=?',(uid,ref))
    def skip_links(self,uid):
        with self.conn() as c:
            r=c.execute('SELECT skip_links FROM settings WHERE user_id=?',(uid,)).fetchone(); return bool(r['skip_links'])
    def toggle_links(self,uid):
        with self.conn() as c: c.execute('UPDATE settings SET skip_links=1-skip_links WHERE user_id=?',(uid,))
    def stat(self,uid):
        with self.conn() as c:
            r=c.execute('SELECT connected,running,sent,failed FROM users WHERE user_id=?',(uid,)).fetchone(); return dict(r) if r else {'connected':0,'running':0,'sent':0,'failed':0}
    def running(self,uid,val):
        with self.conn() as c: c.execute('UPDATE users SET running=? WHERE user_id=?',(int(val),uid))
    def inc(self,uid,field):
        with self.conn() as c: c.execute(f'UPDATE users SET {field}={field}+1 WHERE user_id=?',(uid,))
    def admin(self):
        with self.conn() as c:
            return c.execute('SELECT COUNT(*) total, COALESCE(SUM(connected),0) connected, COALESCE(SUM(running),0) running FROM users').fetchone()
