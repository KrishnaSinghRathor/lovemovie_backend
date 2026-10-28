"""Usage: ADMIN_PASSWORD=... python -m app.scripts.create_admin <username> <email>   (prompts if env unset)"""
import asyncio, getpass, os, sys, time, uuid
from motor.motor_asyncio import AsyncIOMotorClient
from ..core.config import Settings
from ..core.security import hash_password


async def main(username, email):
    pw = os.getenv("ADMIN_PASSWORD") or getpass.getpass("Admin password (min 10 chars): ")
    if len(pw) < 10:
        sys.exit("Password too short")
    s = Settings()
    db = AsyncIOMotorClient(s.mongo_url)[s.db_name]
    await db.users.insert_one({"_id": uuid.uuid4().hex, "username": username, "email": email.lower(),
                               "password_hash": hash_password(pw), "role": "admin", "created_at": time.time()})
    print("Admin created.")

if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:3]))
