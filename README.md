# Daedalus

Telegram üzerinden çalışan, kod yazan kişisel asistan.

> **Durum:** iskelet. Klasör yapısı, bağımlılıklar ve ayar şablonu hazır; `app/` içindeki modüller henüz boş ve çalıştırılabilir bir giriş noktası (`app/main.py`) yok.

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # sonra .env dosyasını doldur
```

## Ayarlar (`.env`)

| Değişken | Anlamı |
|----------|--------|
| `TELEGRAM_TOKEN` | Telegram botunun token'ı (@BotFather'dan alınır) |
| `ALLOWED_USER_ID` | İzin verilen Telegram kullanıcı kimliği |
| `ANTHROPIC_API_KEY` | Anthropic API anahtarı |
| `CLAUDE_MODEL` | Kullanılacak model (şablonda `claude-haiku-5-5`) |

`.env` dosyası `.gitignore` içindedir; token ve anahtarları asla commit'leme.

## Klasörler

```
app/        Uygulama kodu (config, security, agent, handlers; şimdilik boş)
sandbox/    Dockerfile: root olmayan `runner` kullanıcısıyla Python 3.12 kabı
workspace/  Çalışma alanı (içeriği git'e girmez)
data/       Veri (içeriği git'e girmez)
logs/       Kayıtlar (içeriği git'e girmez)
tests/      Testler
```

## Çalıştırma

Giriş noktası (`app/main.py`) yazıldığında:

```bash
source .venv/bin/activate
python -m app.main
```
