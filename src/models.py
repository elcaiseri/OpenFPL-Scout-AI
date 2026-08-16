from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ResponseModel(BaseModel):
    scout_team: List[Dict[str, Any]]
    player_points: List[Dict[str, Any]] = Field(default_factory=list)
    gameweek: int
    strategy: str = "model-ensemble"
    version: str = "5.3.0"
    source: str = "official-fpl"
    credits: str = (
        "OpenFPL Scout AI | Official FPL + FPL Data when available | @elcaiseri, 2026"
    )


class TeamRatingModel(BaseModel):
    entry_id: int
    manager_name: str
    team_name: str
    gameweek: int
    picks_gameweek: int
    rating: int = Field(..., ge=0, le=100)
    grade: str
    projected_points: float
    ai_projected_points: float
    projected_gap: float
    components: Dict[str, float]
    captain: str
    differentials: int
    strengths: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    squad: List[Dict[str, Any]] = Field(default_factory=list)
    strategy: str = "model-ensemble"
    version: str = "5.3.0"
    source: str = "official-fpl"


class OfficialFPLCollectionModel(BaseModel):
    """A normalized collection sourced from one official FPL endpoint."""

    source: str = "official-fpl"
    official_endpoint: str
    count: int
    total: Optional[int] = None
    results: List[Dict[str, Any]] = Field(default_factory=list)


class OfficialFPLItemModel(BaseModel):
    """A normalized resource sourced from one official FPL endpoint."""

    source: str = "official-fpl"
    official_endpoint: str
    data: Dict[str, Any]


class APIEndpointModel(BaseModel):
    methods: List[str]
    path: str
    summary: str
    authentication: str
    source: str


class APITagModel(BaseModel):
    name: str
    description: str
    endpoints: List[APIEndpointModel]


class APICatalogModel(BaseModel):
    message: str
    version: str
    source: str
    documentation: Dict[str, str]
    tags: List[APITagModel]


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
    element_type: Optional[int] = Field(
        None, ge=1, le=4, description="Player position type GK=1, DEF=2, MID=3, FWD=4"
    )
    web_name: Optional[str] = Field(None, description="Player name")
    team_name: Optional[str] = Field(None, description="Team name")
    was_home: Optional[bool] = Field(None, description="Whether the match at home")
