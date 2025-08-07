from typing import List, Dict, Any
from pydantic import BaseModel

class ResponseModel(BaseModel):
    scout_team: List[Dict[str, Any]]
    player_points: List[Dict[str, Any]] = []
    gameweek: int
    version: str = "1.1.0"
    credits: str = "OpenFPL-Scout - Developed by Kassem@elcaiseri.com, @2025"
