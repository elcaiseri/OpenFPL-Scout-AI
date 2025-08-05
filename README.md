# OpenFPL

**OpenFPL** - An AI-powered Fantasy Premier League Scout that leverages machine learning models to predict player performances and optimize your FPL team selections. This project combines historical data, real-time match information, and advanced ML models including Linear Regression, XGBoost, and CatBoost to help you build the best possible team.

## Table of Contents
- [Project Overview](#project-overview)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
   - [Running the FPL Scout](#running-the-fpl-scout)
- [Model Information](#model-information)
- [API Integration](#api-integration)
- [Scripts Explanation](#scripts-explanation)
- [Contributing](#contributing)
- [License](#license)
- [Contact](#contact)

## Project Overview

OpenFPL is an AI Fantasy Premier League Scout that uses machine learning to predict player performances for upcoming gameweeks. The system fetches live match data, processes historical player statistics, and uses an ensemble of trained models to provide accurate predictions and optimal team selections.

**Key Features:**
- Multi-model ensemble (Linear Regression, XGBoost, CatBoost)
- Real-time match data integration via Football Data API
- Asynchronous player prediction processing
- Automated optimal team selection with budget constraints
- Position-based player recommendations
- Captain and vice-captain suggestions

## Project Structure

```
OpenFPL/
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
│   ├── utils.py              # Configuration and utility functions
│   └── logger.py             # Logging configuration
│
├── requirements.txt          # Project dependencies
└── README.md                 # Project documentation
```

## Installation

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

## Configuration

Configure the system through `config/config.yaml`:

- **Models**: Paths to trained model files and metadata
- **Team Mappings**: API team names to FPL team names
- **Categorical Columns**: Player and match features used for predictions
- **API Configuration**: Football Data API settings

## Usage

### Running the FPL Scout

Use the trained models to get player predictions and optimal team selection:

```bash
python src/scout.py
```

**Example Output:**
```
element_type    web_name       team_name        expected_points    role
Goalkeeper      Alisson        Liverpool        5.2
Defender        Alexander-Arnold Liverpool       8.1               captain
Defender        Saliba         Arsenal          6.8
Midfielder      Salah          Liverpool        12.4              vice
Forward         Haaland        Man City         10.9
```

## Model Information

| Model             | Version | Description                    |
|-------------------|---------|--------------------------------|
| Linear Regression | v2.0    | Baseline linear model          |
| XGBoost          | v2.0    | Gradient boosting ensemble     |
| CatBoost         | v1.0    | Categorical boosting model     |

**Model Features:**
- Ensemble predictions for improved accuracy
- Feature importance analysis
- Optimized for FPL player performance prediction

## API Integration

OpenFPL integrates with the Football Data API to fetch:
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

- **`src/scout.py`**: Main FPLScout class that handles predictions, team selection, and API integration
- **`src/utils.py`**: Configuration loading and utility functions
- **`src/logger.py`**: Centralized logging configuration

### Key Classes

**FPLScout**: AI-powered player scout
- Asynchronous player prediction processing
- Real-time match data integration
- Optimal team selection with budget constraints
- Position-based recommendations

## What's New in OpenFPL

- **🤖 AI-Powered Predictions**: Advanced ensemble of Linear Regression, XGBoost, and CatBoost models
- **⚡ Asynchronous Processing**: Fast parallel prediction processing for all players
- **🔴 Live Data Integration**: Real-time match data via Football Data API
- **🎯 Smart Team Selection**: Automated optimal team selection with captain recommendations
- **🏷️ New Branding**: Rebranded as OpenFPL - Open Source Fantasy Premier League AI Scout

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
