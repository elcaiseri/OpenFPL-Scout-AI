from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from pydantic import Field

class ResponseModel(BaseModel):
    scout_team: List[Dict[str, Any]]
    player_points: List[Dict[str, Any]] = []
    gameweek: int
    version: str = "1.1.0"
    credits: str = "OpenFPL-Scout AI - Team Predictions | Developed by Kassem @elcaiseri, 2025"

class PlayerPointsModel(BaseModel):
    """
    PlayerPointsModel represents the points data for a player in a specific gameweek.

    Attributes:
        gameweek (int): Gameweek number (1-38).
        element_type (Optional[int]): Player position type (1-4).
        web_name (Optional[str]): Player web name.
        team_name (Optional[str]): Team name.
        was_home (Optional[bool]): Whether the match was at home.
    """
    gameweek: int = Field(..., ge=1, le=38, description="Gameweek number (1-38)")
    element_type: Optional[int] = Field(None, ge=1, le=4, description="Player position type GK=1, DEF=2, MID=3, FWD=4")
    web_name: Optional[str] = Field(None, description="Player name")
    team_name: Optional[str] = Field(None, description="Team name")
    was_home: Optional[bool] = Field(None, description="Whether the match at home")