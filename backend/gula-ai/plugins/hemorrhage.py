import random
from plugin_base import AIPlugin

class HemorrhageDetectionPlugin(AIPlugin):
    @property
    def name(self) -> str:
        return "Brain Hemorrhage Detection"
        
    @property
    def description(self) -> str:
        return "Detects acute intracranial hemorrhage from CT or MR scans"
        
    @property
    def version(self) -> str:
        return "1.0.0"
        
    def run(self, study_metadata: dict) -> list:
        modality = study_metadata.get("modality", "").upper()
        
        # Hemorrhage is typical on CT or MR brain scans
        if modality not in ["CT", "MR"]:
            return []
            
        # Simulate model decision
        prob = random.uniform(0.01, 0.98)
        value = "Positive" if prob > 0.65 else "Negative"
        
        return [{
            "resourceType": "Observation",
            "code": "brain-hemorrhage",
            "value": value,
            "probability": round(prob, 4)
        }]
