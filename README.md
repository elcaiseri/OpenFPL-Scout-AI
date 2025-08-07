# OpenFPL-Scout-AI

**OpenFPL-Scout-AI** is an AI-powered Fantasy Premier League Scout leveraging machine learning models to predict player performances and optimize your FPL team selections. This project combines historical data, real-time match information, and advanced ML models (Linear Regression, XGBoost, CatBoost) to help you build the best possible team.

## Table of Contents
- [Project Overview](#project-overview)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [Running the FPL Scout API](#running-the-fpl-scout-api)
  - [API Endpoints](#api-endpoints)
- [Model Information](#model-information)
- [API Integration](#api-integration)
- [Scripts Explanation](#scripts-explanation)
- [Contributing](#contributing)
- [License](#license)
- [Contact](#contact)

## Project Overview

OpenFPL-Scout-AI uses machine learning to predict player performances for upcoming gameweeks. The system fetches live match data, processes historical player statistics, and uses an ensemble of trained models to provide accurate predictions and optimal team selections.

**Key Features:**
- Multi-model ensemble (Linear Regression, XGBoost, CatBoost)
- Real-time match data integration via Football Data API
- Asynchronous player prediction processing
- Automated optimal team selection with budget constraints
- Position-based player recommendations
- Captain and vice-captain suggestions

## Project Structure

```
OpenFPL-Scout-AI/
│
├── config/                   # Configuration files
│   └── config.yaml           # Model paths, team mappings, API settings
│
├── data/                     # Data storage
│   └── external/             # External data from FPL sources
│       └── *.csv             # Player statistics files
│
├── models/                   # Trained ML models
│   ├── reg_0.pkl             # Linear Regression pipeline
│   ├── reg_1.pkl             # XGBoost pipeline
│   └── reg_2.pkl             # CatBoost pipeline
│
├── src/                      # Source code
│   ├── scout.py              # Main FPLScout class for predictions
│   ├── models.py             # Pydantic response models
│   ├── utils.py              # Configuration and utility functions
│   └── logger.py             # Logging configuration
│
├── main.py                   # FastAPI application entry point
├── requirements.txt          # Project dependencies
└── README.md                 # Project documentation
```

## Installation

### Local Development

1. **Clone the repository**:
   ```bash
   git clone https://github.com/elcaiseri/Fantasy-Premier-League-LTX.git
   cd Fantasy-Premier-League-LTX
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up API Key**:
   ```bash
   export FPL_API_KEY="your_football_data_api_key"
   ```

### Docker Deployment

1. **Clone the repository**:
   ```bash
   git clone https://github.com/elcaiseri/Fantasy-Premier-League-LTX.git
   cd Fantasy-Premier-League-LTX
   ```

2. **Build and run with Docker**:
   ```bash
   docker build -t openfpl-scout-ai .
   docker run -p 8000:8000 -e FPL_API_KEY="your_api_key" openfpl-scout-ai
   ```

## Configuration

Configure the system through `config/config.yaml`:

- **Models**: Paths to trained model files and metadata
- **Team Mappings**: API team names to FPL team names
- **Categorical Columns**: Player and match features used for predictions
- **API Configuration**: Football Data API settings

## Usage

### Running the FPL Scout API

#### Option 1: Direct Python Execution
Start the FastAPI server to access the FPL Scout via REST API:

```bash
python main.py
```

Or using uvicorn directly:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

#### Option 2: Docker Deployment
Build and run the application using Docker:

```bash
# Build the Docker image
docker build -t openfpl-scout-ai .

# Run the container
docker run -d \
  --name openfpl-api \
  -p 8000:8000 \
  -e FPL_API_KEY="your_football_data_api_key" \
  openfpl-scout-ai

# Check container status
docker ps

# View logs
docker logs openfpl-api
```

#### Option 3: Docker Compose (Recommended)
Create a `docker-compose.yml` file:

```yaml
version: '3.8'
services:
  openfpl-scout-ai:
   build: .
   ports:
    - "8000:8000"
   environment:
    - FPL_API_KEY=your_football_data_api_key
   volumes:
    - ./data:/app/data
    - ./models:/app/models
    - ./config:/app/config
   restart: unless-stopped
   healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

Then run:
```bash
docker-compose up -d
```

The API will be available at `http://localhost:8000`

### API Endpoints

- **GET /** - Root endpoint with API information
- **GET /health** - Health check and API status
- **GET /scout-team** - Get optimal team for current gameweek
- **GET /scout-report** - Get all player predictions for current gameweek

**Example API Calls:**
```bash
# Get optimal team
curl http://localhost:8000/scout-team

# Get all player predictions
curl http://localhost:8000/scout-report

# Check API health
curl http://localhost:8000/health
```

**Example API Response for /scout-team:**
```json
{
  "content": [
   {
    "element_type": "Goalkeeper",
    "web_name": "Alisson",
    "team_name": "Liverpool",
    "expected_points": 5.2,
    "role": null
   },
   {
    "element_type": "Defender",
    "web_name": "Alexander-Arnold",
    "team_name": "Liverpool",
    "expected_points": 8.1,
    "role": "captain"
   }
  ],
  "version": "1.0.0",
  "credits": "OpenFPL-Scout-AI - Developed by Kassem@elcaiseri, 2025"
}
```

**Endpoint Names Explained:**
- 🏆 `/scout-team` - Your optimal FPL team selection
- 📊 `/scout-report` - Comprehensive player analysis and predictions
- 🏥 `/health` - API health check

Access the interactive API documentation at `http://localhost:8000/docs`

## Model Information

| Model             | Version | Description                    |
|-------------------|---------|--------------------------------|
| Linear Regression | v2.0    | Baseline linear model          |
| XGBoost           | v2.0    | Gradient boosting ensemble     |
| CatBoost          | v1.0    | Categorical boosting model     |

**Model Features:**
- Ensemble predictions for improved accuracy
- Feature importance analysis
- Optimized for FPL player performance prediction

## API Integration

OpenFPL-Scout-AI integrates with the Football Data API to fetch:
- Live match fixtures
- Team vs team matchups
- Home/away status
- Gameweek information

**Required Environment Variable:**
```bash
FPL_API_KEY=your_api_key_here
```

## Scripts Explanation

### Core Components

- **`main.py`**: FastAPI application entry point with REST endpoints for FPL predictions
- **`src/scout.py`**: Main FPLScout class that handles predictions, team selection, and API integration
- **`src/models.py`**: Pydantic response models for API data validation
- **`src/utils.py`**: Configuration loading and utility functions
- **`src/logger.py`**: Centralized logging configuration

### Key Classes

**FPLScout**: AI-powered player scout
- Asynchronous player prediction processing
- Real-time match data integration
- Optimal team selection with budget constraints
- Position-based recommendations

**ResponseModel**: Pydantic model for standardized API responses
- Structured JSON responses with content, version, and credits
- Type validation and serialization

## What's New in OpenFPL-Scout-AI

- **🎯 Fine-Tuned for 2024/2025 Season**: Models optimized and trained on the latest Premier League season data for maximum accuracy
- **🚀 CatBoost Integration**: Enhanced machine learning pipeline with CatBoost algorithm implementation (addressing [GitHub Issue #1](https://github.com/elcaiseri/Fantasy-Premier-League-LTX/issues/1))
- **🔌 RESTful API**: Complete FastAPI implementation with endpoints for team selection and player predictions
- **🏷️ Rebranding**: Complete rebrand from Fantasy-Premier-League-LTX to OpenFPL - Open Source Fantasy Premier League AI Scout
- **🔧 Code Refactoring**: Improved code structure, modularity, and maintainability with proper separation of concerns
- **🤖 AI-Powered Predictions**: Advanced ensemble of Linear Regression, XGBoost, and CatBoost models
- **⚡ Asynchronous Processing**: Fast parallel prediction processing for all players
- **🔴 Live Data Integration**: Real-time match data via Football Data API
- **🐳 Docker Support**: Containerized deployment for easy setup and scalability

## Contributing

Contributions are welcome! Areas for improvement:

- **Enhanced Algorithms**: Improve prediction accuracy and team selection logic
- **Web Interface**: Develop a user-friendly web application
- **Mobile App**: Create mobile applications for iOS/Android
- **Additional Features**: Player injury tracking, form analysis, fixture difficulty
- **Documentation**: Improve code documentation and tutorials

Please fork the repository, create a feature branch, and submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Contact

For questions or support, contact [kassem@elcaiseri.com](mailto:kassem@elcaiseri.com).
