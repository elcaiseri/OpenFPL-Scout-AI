# OpenFPL-Scout-AI

OpenFPL-Scout-AI is an AI-powered Fantasy Premier League Scout that uses ensemble machine learning (Linear Regression, XGBoost, CatBoost) to predict player points and optimize FPL team selection. It leverages historical stats, live fixtures, and advanced ML models.

## Features

- Ensemble predictions (Linear Regression, XGBoost, CatBoost)
- Real-time fixture and match data integration
- Fast, async player predictions
- Automated optimal team selection (by position)
- Captain/vice-captain assignment

## Installation

**Docker:**
```bash
docker build -t openfpl-scout-ai .
docker run -d -p 8000:8000 --name openfpl-api openfpl-scout-ai
```

## Usage

API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

### Endpoints

- `POST /scout` — Upload FPL stats CSV, receive optimal team and predictions
- `GET /health` — Health check

**Sample `/scout` response:**
```json
{
  "scout_team": [
    {
      "element_type": "Goalkeeper",
      "web_name": "Alisson",
      "team_name": "Liverpool",
      "expected_points": 5.2,
      "role": ""
    },
    {
      "element_type": "Defender",
      "web_name": "Alexander-Arnold",
      "team_name": "Liverpool",
      "expected_points": 8.1,
      "role": "captain"
    }
  ],
  "player_points": [],
  "gameweek": 1,
  "version": "1.1.0",
  "credits": "OpenFPL-Scout-AI - Developed by Kassem@elcaiseri.com, 2025"
}
```

## Model Overview

| Model             | Version | Description                    |
|-------------------|---------|--------------------------------|
| Linear Regression | v2.0    | Baseline linear model          |
| XGBoost           | v2.0    | Gradient boosting ensemble     |
| CatBoost          | v1.0    | Categorical boosting model     |

- Ensemble predictions for accuracy
- Feature importance analysis
- Optimized for FPL player performance

## API Integration

Integrates with Football Data API for:
- Live fixtures and matchups
- Home/away status
- Gameweek info

**Required Environment Variable:**
```bash
FPL_API_KEY=your_api_key_here
```

## Code Structure

- `main.py`: FastAPI app and endpoints
- `src/scout.py`: FPLScout class (predictions, team selection)
- `src/models.py`: Pydantic response models
- `src/utils.py`: Config and helpers
- `src/logger.py`: Logging

## What's New

- **2024/2025 Season**: Models updated with latest data
- **CatBoost Integration**: Improved ML pipeline ([Issue #1](https://github.com/elcaiseri/Fantasy-Premier-League-LTX/issues/1))
- **RESTful API**: FastAPI endpoints for team selection and predictions
- **Rebranding**: Now OpenFPL-Scout-AI
- **Refactored Code**: Improved modularity and maintainability
- **AI-Powered Predictions**: Advanced ensemble models
- **Async Processing**: Fast parallel predictions
- **Live Data**: Real-time match integration
- **Docker Support**: Easy deployment

## Contributing

Contributions welcome! Ideas for improvement:
- Enhanced algorithms and selection logic
- Web/mobile interfaces
- Player injury/form tracking
- Better documentation

Fork, branch, and submit a pull request.

## License

MIT License — see [LICENSE](LICENSE) for details.

## Contact

Questions/support: [kassem@elcaiseri.com](mailto:kassem@elcaiseri.com)
