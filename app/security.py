from cryptography.fernet import Fernet

class SecretBox:
    def __init__(self,key:bytes): self.cipher=Fernet(key)
    def encrypt(self,value:str)->bytes: return self.cipher.encrypt(value.encode())
    def decrypt(self,value:bytes)->str: return self.cipher.decrypt(value).decode()
