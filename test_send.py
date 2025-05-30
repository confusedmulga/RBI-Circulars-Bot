import requests

BOT_TOKEN = "7273132456:AAGnfGLSdd16FWSvUH-v1A2_v-JYCLJsXLE"
CHAT_ID   = "-1002526785455"

def test():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": "RBI Circular Bot Test Complete"}
    resp = requests.post(url, data=data)
    print(resp.status_code, resp.text)

if __name__ == "__main__":
    test()
