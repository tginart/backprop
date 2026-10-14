"""Pinned compatibility hook retained for Keras callback imports."""

def patch_tf_keras():
    """Current supported tf.keras callbacks require no global monkeypatch."""
    return None
