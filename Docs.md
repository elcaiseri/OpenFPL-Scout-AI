# OpenFPL Scout API – Backend Documentation

Version: 2.3.1

Description: AI-powered Fantasy Premier League (FPL) Scout API providing optimal team selection, player projections, and gameweek analysis.

---

## Authentication

- Type: Bearer Token
- Header example:
    ```
    Authorization: Bearer <API_TOKEN>
    ```
- Store <API_TOKEN> in environment variables or a secret manager (never hard-code).

---

## Endpoints

### 1) Health Check
- Method: GET
- Path: `/api/health`
- Purpose: Service availability check
- Response (200): `"ok"`

---

### 2) Get Scout Team
- Method: POST
- Path: `/api/scout`
- Description: Upload FPL team data file to receive an AI-optimized scout team and player projections.

Request
- Headers:
    - `accept: application/json`
    - `Authorization: Bearer <API_TOKEN>`
    - `Content-Type: multipart/form-data`
- Body:
    - `file` (required): CSV or JSON containing FPL stats

---

## Responses

### ✅ 200 OK
```json
{
    "scout_team": [
        {
            "element_type": "Defender",
            "web_name": "Calafiori",
            "team_name": "Arsenal",
            "opponent_team_name": "Liverpool",
            "was_home": false,
            "gameweek": 3,
            "expected_points": 12.845314870228341,
            "role": "captain"
        }
    ],
    "player_points": [
        {
            "element_type": 1,
            "web_name": "A.Becker",
            "team_name": "Liverpool",
            "opponent_team_name": "Arsenal",
            "was_home": true,
            "gameweek": 3,
            "expected_points": 1.0170301728
        }
    ],
    "gameweek": 3,
    "version": "2.3.1",
    "credits": "OpenFPL-Scout AI - Developed by Kassem@elcaiseri.com, @2025"
}
```

### ❌ 422 Validation Error
```json
{
    "detail": [
        {
            "loc": ["body", "file"],
            "msg": "Invalid file format",
            "type": "value_error"
        }
    ]
}
```

---

## Schema

### scout_team[]
| Field               | Type    | Description                                              |
|---------------------|---------|----------------------------------------------------------|
| element_type        | string  | Player position (Goalkeeper, Defender, Midfielder, Forward) |
| web_name            | string  | Short player name                                        |
| team_name           | string  | Player’s club                                            |
| opponent_team_name  | string  | Opponent club                                            |
| was_home            | boolean | True if player is playing at home                        |
| gameweek            | integer | Current gameweek                                         |
| expected_points     | float   | Predicted points for this match                          |
| role                | string  | Squad role (captain, vice, or empty)                     |

### player_points[]
| Field               | Type    | Description                                 |
|---------------------|---------|---------------------------------------------|
| element_type        | integer | Player type ID (1 = GK, 2 = DEF, 3 = MID, 4 = FWD) |
| web_name            | string  | Short player name                           |
| team_name           | string  | Player’s club                               |
| opponent_team_name  | string  | Opponent club                               |
| was_home            | boolean | True if home match                          |
| gameweek            | integer | Gameweek number                             |
| expected_points     | float   | Predicted points                            |

---

## Example Requests

cURL
```bash
curl -X POST "https://openfpl-scout-ai-186049008266.europe-west1.run.app/api/scout" \
    -H "accept: application/json" \
    -H "Authorization: Bearer <API_TOKEN>" \
    -H "Content-Type: multipart/form-data" \
    -F "file=@fpl-data-stats.csv;type=text/csv"
```

Python
```python
import requests

url = "https://openfpl-scout-ai-186049008266.europe-west1.run.app/api/scout"
headers = {
        "Authorization": "Bearer <API_TOKEN>",
        "accept": "application/json"
}
files = {"file": open("fpl-data-stats.csv", "rb")}

response = requests.post(url, headers=headers, files=files)
print(response.json())
```

---

## Security Notes
- Always use a Bearer token from environment variables or a secret manager.
- Rotate tokens regularly.
- Enforce HTTPS for all requests.
