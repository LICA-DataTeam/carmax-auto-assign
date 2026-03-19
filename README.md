# carmax-auto-assign API

## Flow
1. Accepts HTTP request from CarMax LiveAgent automated rule:
    - **HTTP Body (Sample request)**:
    ```
    POST /auto-assign
    Content-Type: application/x-www-form-urlencoded
    conv_code=123&agent_id=agent_id123
    ```

2. `carmax-auto-assign` API logs the inbound request with `conv_code` and `agent_id`.

3. **Idempotency check**
    - If the ticket already has an assignment in state storage, return:
        - `status: "already_assigned"`

4. **Existing agent check**
    - If LiveAgent already assigned the ticket, API returns:
        - `status: "already_assigned"`

5. **Time Window**
    - Runs from **08:30 to 24:00 (Asia/Manila)**.
    - Outside time window, return:
        - `status: "outside_hours"`

6. **Load agents**
    - Stored in static JSON file (`config/`)

7. **Auto-Assign logic and requirements**
    - Each agent receives up to **30 auto-assignments per day**.
    - **Round-robin** selection
        - Select next eligible agent in order.

## Response structure
```json
{ "status": "assigned", "conv_code": "123", "agent_id": "i3gpqj30", "reason": "round_robin" }

```

### Setup
Create a `.env` file and copy the contents of `.env.example`. Then supply the needed credentials.

```bash
touch .env
cp .env.example .env # or cat .env.example > .env
```

### Install and run locally
```bash
pip install -r requirements.txt
# using uvicorn
uvicorn src.api.app:app --reload
```

### Tests
```bash
pytest -q
```
