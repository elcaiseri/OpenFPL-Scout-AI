from typing import List, Dict, Any
from pydantic import BaseModel

class ResponseModel(BaseModel):
    content: List[Dict[str, Any]]
    version: str = "1.0.0"
    credits: str = "OpenFPL - Developed by Kassem@elcaiseri, 2025"
