"""Notebook presentation shared by read-only experiment objects.

Architecture: check active-run silence and notebook presence -> build Foundry
HTML link/iframe -> hand it to IPython when requested. Normal Python execution
does not import IPython or print output. Main callers: Run/Project/File.display.
"""
from ...settings import in_notebook


class Displayable:
    def snake_to_camel(self, string):
        camel = "".join(piece.title() for piece in string.split("_"))
        return camel[0].lower() + camel[1:]

    def display(self, height=420, hidden=False):
        """Display the object in a notebook; return whether anything was shown."""
        import backprop
        if backprop.run is not None and backprop.run._settings.silent: return False
        if not in_notebook(): return False
        markup = self.to_html(height, hidden)
        if markup is None: return False
        try:
            from IPython import display
        except ImportError:
            return False
        display.display(display.HTML(markup))
        return True

    def to_html(self, *args, **kwargs): return None
