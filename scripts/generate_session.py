import asyncio
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

async def main():
    api_id = int(input("Enter API_ID: "))
    api_hash = input("Enter API_HASH: ")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nYour TELEGRAM_STRING_SESSION is:\n")
        print(client.session.save())
        print("\nStore this securely in your Railway Environment Variables.")

if __name__ == "__main__":
    asyncio.run(main())
