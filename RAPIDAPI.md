# OpenFPL API

OpenFPL is an AI-powered Fantasy Premier League API for player projections,
Gameweek planning, squad recommendations, and normalized official FPL data.

Version **6.0.0** combines live Fantasy Premier League information with a
four-model ensemble—Ridge, XGBoost, CatBoost, and MLP—to estimate player points
and select a complete 15-player squad with captain and vice-captain choices.

## Base URL

```text
https://openfpl-api.p.rapidapi.com
```

## Authentication

Every request made through RapidAPI must include the RapidAPI key and host
headers shown in the API playground:

```http
X-RapidAPI-Key: YOUR_RAPIDAPI_KEY
X-RapidAPI-Host: openfpl-api.p.rapidapi.com
```

You do not need to create a separate OpenFPL API key when using the API through
RapidAPI.

## Quick start

Generate the recommended squad for Gameweek 1:

```bash
curl --request GET \
  --url 'https://openfpl-api.p.rapidapi.com/api/scout?gameweek=1' \
  --header 'X-RapidAPI-Key: YOUR_RAPIDAPI_KEY' \
  --header 'X-RapidAPI-Host: openfpl-api.p.rapidapi.com'
```

If `gameweek` is omitted from `/api/scout`, OpenFPL selects the current or next
playable Gameweek from the official FPL event state.

## What you can build

- Gameweek planning and player-comparison tools
- AI squad and captain recommendation applications
- FPL manager team-rating tools
- Fixture, availability, price, and ownership dashboards
- Live scoring, league, ranking, and Dream Team integrations
- Bots, mobile applications, data pipelines, and analytics products

## Scout AI endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/scout` | Generate a 15-player squad for the requested or upcoming Gameweek |
| `GET` | `/api/scout/team-rating` | Rate a public FPL manager squad from 0 to 100 |
| `POST` | `/api/scout` | Generate a squad with the full player-projection payload |
| `GET` | `/api/gw/scout` | Generate a squad for a required Gameweek |
| `GET` | `/api/gw/playerpoints` | Generate and filter player point projections |

### Generate a squad

```http
GET /api/scout?gameweek=1
```

The response contains the selected squad, Gameweek, inference strategy, data
source, and API version:

```json
{
  "scout_team": [
    {
      "id": 123,
      "web_name": "Player Name",
      "team_name": "Club Name",
      "element_type": "Midfielder",
      "expected_points": 6.42,
      "now_cost": 8.5,
      "selected_by_percent": 18.7,
      "role": "captain"
    }
  ],
  "player_points": [],
  "gameweek": 1,
  "strategy": "model-ensemble",
  "version": "6.0.0",
  "source": "official-fpl"
}
```

Fields within player records can evolve as official FPL data changes. Treat the
live endpoint response and the RapidAPI endpoint schema as authoritative.

### Rate a manager team

```http
GET /api/scout/team-rating?entry_id=1234567&gameweek=1
```

`entry_id` is the public numeric FPL team ID. The result includes an overall
rating, grade, projected score, comparison with the AI benchmark, captaincy and
availability components, strengths, risks, and the published squad.

FPL controls when manager picks become public. Current picks may be unavailable
until the relevant Gameweek deadline has passed.

### Filter player projections

```http
GET /api/gw/playerpoints?gameweek=1&element_type=3&team_name=Liverpool
```

| Parameter | Type | Required | Description |
|---|---|---:|---|
| `gameweek` | integer | Yes | Gameweek from 1 to 38 |
| `element_type` | integer | No | Position: goalkeeper `1`, defender `2`, midfielder `3`, forward `4` |
| `web_name` | string | No | Exact player web-name match, case-insensitive |
| `team_name` | string | No | Exact normalized club-name match, case-insensitive |
| `was_home` | boolean | No | Filter by home or away fixture |

## Official FPL data endpoints

OpenFPL maps official FPL responses into stable, application-friendly JSON.

### Gameweeks and service

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api` | Discover every route grouped by resource |
| `GET` | `/api/health` | Check service, model, and data-source health |
| `GET` | `/api/gameweeks` | List completed and currently playable Gameweeks |
| `GET` | `/api/fpl/gameweeks` | List all official Gameweek records |
| `GET` | `/api/fpl/gameweeks/status` | Get bonus processing and league-update status |
| `GET` | `/api/fpl/gameweeks/{gameweek}/live` | Get live player scoring for a Gameweek |
| `GET` | `/api/fpl/dream-team` | Get the published season Dream Team |
| `GET` | `/api/fpl/gameweeks/{gameweek}/dream-team` | Get a published Gameweek Dream Team |

### Players, clubs, and fixtures

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/fpl/teams` | List clubs and official strength ratings |
| `GET` | `/api/fpl/players` | Search and filter players, prices, availability, and totals |
| `GET` | `/api/fpl/players/{player_id}` | Get one player |
| `GET` | `/api/fpl/players/{player_id}/history` | Get match history, upcoming fixtures, and past seasons |
| `GET` | `/api/fpl/fixtures` | Filter fixtures by Gameweek, club, future state, or completion |
| `GET` | `/api/fpl/fixtures/{fixture_id}/stats` | Get official per-player fixture statistics |

`GET /api/fpl/players` supports:

- `team_id`
- `element_type`
- `selectable_only`
- `status`
- `search`
- `min_price` and `max_price`
- `order_by` and `descending`
- `offset` and `limit`

Player prices returned by mapped official endpoints are expressed in millions.

`GET /api/fpl/fixtures` supports `gameweek`, `team_id`, `future_only`, and
`finished`.

### Managers, leagues, and rankings

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/fpl/managers/{entry_id}` | Get a public manager profile |
| `GET` | `/api/fpl/managers/{entry_id}/history` | Get season, chip, and past-season history |
| `GET` | `/api/fpl/managers/{entry_id}/transfers` | Get published transfers |
| `GET` | `/api/fpl/managers/{entry_id}/gameweeks/{gameweek}/picks` | Get published Gameweek picks |
| `GET` | `/api/fpl/leagues/classic/{league_id}/standings` | Get classic-league standings |
| `GET` | `/api/fpl/leagues/h2h/{league_id}/standings` | Get head-to-head standings |
| `GET` | `/api/fpl/leagues/h2h/{league_id}/matches` | Get head-to-head matches |
| `GET` | `/api/fpl/leagues/{league_id}/cup-status` | Get league cup status |
| `GET` | `/api/fpl/regions` | List manager regions |
| `GET` | `/api/fpl/set-piece-notes` | Get club set-piece notes |
| `GET` | `/api/fpl/rankings/best-private-leagues` | Get the best private leagues ranking |
| `GET` | `/api/fpl/rankings/most-valuable-teams` | Get the most valuable teams ranking |
| `GET` | `/api/fpl/gameweeks/{gameweek}/winners` | Get published Gameweek winners |
| `GET` | `/api/fpl/phases/{phase_id}/winners` | Get published phase winners |

## Examples

### JavaScript

```javascript
const url = new URL("https://openfpl-api.p.rapidapi.com/api/fpl/players");
url.search = new URLSearchParams({
  element_type: "3",
  selectable_only: "true",
  order_by: "total_points",
  descending: "true",
  limit: "20"
}).toString();

const response = await fetch(url, {
  headers: {
    "X-RapidAPI-Key": "YOUR_RAPIDAPI_KEY",
    "X-RapidAPI-Host": "openfpl-api.p.rapidapi.com"
  }
});

if (!response.ok) {
  throw new Error(`OpenFPL request failed: ${response.status}`);
}

const data = await response.json();
console.log(data.results);
```

### Python

```python
import requests

url = "https://openfpl-api.p.rapidapi.com/api/fpl/fixtures"
headers = {
    "X-RapidAPI-Key": "YOUR_RAPIDAPI_KEY",
    "X-RapidAPI-Host": "openfpl-api.p.rapidapi.com",
}
params = {"gameweek": 1}

response = requests.get(url, headers=headers, params=params, timeout=30)
response.raise_for_status()

for fixture in response.json()["results"]:
    print(fixture["home_team"]["name"], "vs", fixture["away_team"]["name"])
```

## Response formats

Most mapped collection endpoints use this envelope:

```json
{
  "source": "official-fpl",
  "official_endpoint": "/fixtures/",
  "count": 10,
  "total": 10,
  "results": []
}
```

Single-resource endpoints use:

```json
{
  "source": "official-fpl",
  "official_endpoint": "/element-summary/123/",
  "data": {}
}
```

## Error responses

FastAPI errors use a JSON `detail` field:

```json
{
  "detail": "Description of the error"
}
```

| Status | Meaning |
|---:|---|
| `400` | The request could not be processed |
| `401` or `403` | Authentication failed or required credentials are missing |
| `404` | The requested player, manager, league, or published resource was not found |
| `422` | Query validation failed or a projection could not be produced |
| `429` | The active RapidAPI plan's request limit was exceeded |
| `500` | OpenFPL encountered an internal configuration or service error |
| `502` | The upstream official FPL service failed or was unavailable |

## Data and prediction notes

- Official FPL is the primary source for players, clubs, fixtures, event state,
  live scores, public managers, leagues, and rankings.
- Player projections use recent form, expected performance, minutes,
  availability, ownership, and fixture context when those inputs are available.
- Missing historical match statistics may be enriched from a guarded secondary
  source from Gameweek 2 onward. Official values are never replaced.
- Before Gameweek 1 has match history, the models use a controlled cold-start
  strategy based on the available official data.
- The squad selector enforces official positional quotas and a maximum of three
  players from one club.
- Squad selection is currently **budget-free**. Prices are returned for context
  but are not used as an optimization constraint.
- Predictions are estimates, not guarantees of future performance.

## Support and links

- Website: [openfpl.kassem.dev](https://openfpl.kassem.dev)
- API reference: [openfpl.kassem.dev/redoc](https://openfpl.kassem.dev/redoc)
- Source code: [github.com/elcaiseri/OpenFPL-Scout-AI](https://github.com/elcaiseri/OpenFPL-Scout-AI)
- Email: [iqasem4444@gmail.com](mailto:iqasem4444@gmail.com)

## Disclaimer

OpenFPL is an independent, open-source project. It is not affiliated with,
endorsed by, or sponsored by the Premier League or Fantasy Premier League.
Fantasy Premier League names and data remain the property of their respective
owners.

OpenFPL is provided for informational and entertainment purposes. Review the
API plan limits and terms before using it in a production application.

---

OpenFPL API v6.0.0 · Updated 22 August 2026
