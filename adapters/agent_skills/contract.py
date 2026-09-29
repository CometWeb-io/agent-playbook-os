"""Source-checkout compatibility import; use the installed package API instead."""
from agent_playbook_os.adapters.agent_skills import AgentSkillsHost, AgentSkillsRuntime, SkillInvocation

__all__ = ["AgentSkillsHost", "AgentSkillsRuntime", "SkillInvocation"]
