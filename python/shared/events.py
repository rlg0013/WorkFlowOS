from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from datetime import datetime
import uuid
import json


@dataclass
class Event:
    source: str
    action: str
    target: str
    payload: str = ""
    session_id: str = ""
    timestamp: str = ""
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat() + "Z"
        if not self.session_id:
            self.session_id = f"session_{datetime.utcnow().strftime('%Y%m%d')}"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "Event":
        d = json.loads(data)
        return cls(**d)


@dataclass
class WorkflowSpec:
    intent: str
    trigger: Dict[str, str]
    actions: list
    conditions: list
    variables: Dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowSpec":
        return cls(
            intent=data.get("intent", ""),
            trigger=data.get("trigger", {}),
            actions=data.get("actions", []),
            conditions=data.get("conditions", []),
            variables=data.get("variables", {}),
        )