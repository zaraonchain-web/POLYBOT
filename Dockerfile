FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["pytest", "tests/", "-v"]
```

This tells Railway to **run the tests** instead of the trading bot — no real trades, no real keys needed.

### Step 3: Set up on Railway

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your forked repo
3. Railway will auto-detect the `Dockerfile`

### Step 4: Add environment variables (dummy values for testing)

In Railway → **Variables**, add these (fake values are fine for unit tests):
```
POLY_PRIVATE_KEY=0000000000000000000000000000000000000000000000000000000000000001
POLY_SAFE_ADDRESS=0x0000000000000000000000000000000000000001
