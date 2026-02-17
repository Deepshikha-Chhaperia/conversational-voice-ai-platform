# Plug-and-Play Voice Bot

This repository contains the voice bot platform. This guide explains how to navigate, run, and trigger calls.


## 1. How to Navigate the Code

For API keys (Deepgram, Cerebras, Vobiz, etc.), just copy the `.env.example` file to a new file named `.env` and fill in your keys there.

If you want to switch AI providers (like changing from Deepgram to something else), just change the `active_providers` list at the top of `config.yaml`. That's also where you can tweak model settings and voices.


## 2. How to Run the Server

You need **three separate terminals** to run the full stack locally.

### Terminal 1: Start the Backend
Runs the FastAPI server (bot logic).
```bash
python main.py
```

### Terminal 2: Expose to Internet
Creates a secure tunnel so Vobiz (telephony provider) can reach your localhost.
```bash
cloudflared tunnel --url http://localhost:8000
```
**Copy the URL** (e.g., `https://random-name.trycloudflare.com`) from this terminal. You will need it for triggering calls.


## 3. Trigger an Outbound Call

There are two ways: **Terminal** (quick testing) or **Postman** (proper API testing). Both do the same thing.

### Method 1: Terminal (Quick Test)
Run this command in **Terminal 3**. Replace `YOUR-TUNNEL-URL` with the one from Terminal 2.

**Standard Call (Uses `config.yaml` prompt):**
```bash
python -c "import requests; print(requests.post('https://YOUR-TUNNEL-URL/outbound', json={'to': '+919876543210', 'customer_name': 'Rahul'}).text)"
```

**Custom Campaign Call (Overrides default prompt):**
```bash
python -c "import requests; print(requests.post('https://YOUR-TUNNEL-URL/outbound', json={'to': '+919876543210', 'customer_name': 'Priya', 'campaign_prompt': 'You are calling on behalf of XYZ Corp. Ask if they need office space.'}).text)"
```

### Method 2: Postman (Visual Testing)
1.  Open Postman and create a new request.
2.  Set method to **POST**.
3.  Set URL to: `https://YOUR-TUNNEL-URL/outbound`
4.  Go to the **Body** tab.
5.  Select **raw** and change the dropdown from "Text" to **JSON**.
6.  Paste this JSON body:
    ```json
    {
        "to": "+919876543210",
        "customer_name": "Rahul"
    }
    ```
7.  Click **Send**.
8.  **Success**: You should see `{"status": "success", ...}` and your phone will ring.

**Advanced Options (JSON fields):**
-   `to`: (Required) The phone number to call.
-   `customer_name`: (Optional) Name to use in the greeting. Defaults to "there".
-   `campaign_prompt`: (Optional) Overrides the campaign script in `config.yaml`.
-   `greeting`: (Optional) Overrides the initial greeting.


## 4. Key Features for Demo

-   **Latency**: The bot responds in **< 4 seconds** (optimized with global VAD).
-   **Smart Hangup**:
    -   Says "Goodbye" properly before hanging up.
    -   Handles internal signals (`[end_call]`) instantly.
