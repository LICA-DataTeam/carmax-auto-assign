# carmax-auto-assign API

## Configuration
- To add an agent or a new team, you have two options:

### Option 1 (via file)
- Edit `config/carmax_agents.json`.

### Option 2 (via admin)
- Click `Authorize` via Swagger docs (`/docs`)
- Paste the bearer token
- Get the current version by making a `GET` request to `/admin/agents`.
- Copy the version
- Create agent: Execute `POST /admin/agents`
- Use body like:
```json
{
  "team": "Team C",
  "agent_key": "abc123xy",
  "agent_name": "Jane Doe",
  "active": true,
  "expected_version": 12,
  "change_reason": "Add new support agent",
  "target": 30,
  "min": 30,
  "max": 30
}
```

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
    - Runs from **08:30 to 17:30 (Asia/Manila)**.
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
