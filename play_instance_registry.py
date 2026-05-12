"""
Global registry for accessing Play instance from webapp
"""

_play_instance = None

def register_play_instance(play_instance):
    """Register the global Play instance"""
    global _play_instance
    _play_instance = play_instance

def get_play_instance():
    """Get the global Play instance"""
    return _play_instance

def clear_play_instance():
    """Clear the global Play instance"""
    global _play_instance
    _play_instance = None
