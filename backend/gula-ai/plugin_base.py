from abc import ABC, abstractmethod

class AIPlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass
        
    @property
    @abstractmethod
    def description(self) -> str:
        pass
        
    @property
    @abstractmethod
    def version(self) -> str:
        pass
        
    @abstractmethod
    def run(self, study_metadata: dict) -> list:
        """
        Runs mock inference on study metadata.
        Returns a list of FHIR-aligned Observation dicts.
        """
        pass
