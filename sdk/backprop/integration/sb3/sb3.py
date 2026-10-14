# Adapted from W&B 0.30.0 (MIT); see THIRD_PARTY_NOTICES.md.
# Architecture: framework callbacks -> Backprop public logging -> Foundry.
# Main callers: the corresponding optional training framework.
"""W&B callback for sb3.

Really simple callback to get logging for each tree

Example usage:

```python
import gym
from stable_baselines3 import PPO
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import DummyVecEnv, VecVideoRecorder
import wandb
from wandb.integration.sb3 import WandbCallback


config = {
    "policy_type": "MlpPolicy",
    "total_timesteps": 25000,
    "env_name": "CartPole-v1",
}
run = wandb.init(
    project="sb3",
    config=config,
    sync_tensorboard=True,  # auto-upload sb3's tensorboard metrics
    monitor_gym=True,  # auto-upload the videos of agents playing the game
    save_code=True,  # optional
)


def make_env():
    env = gym.make(config["env_name"])
    env = Monitor(env)  # record stats such as returns
    return env


env = DummyVecEnv([make_env])
env = VecVideoRecorder(
    env, "videos", record_video_trigger=lambda x: x % 2000 == 0, video_length=200
)
model = PPO(config["policy_type"], env, verbose=1, tensorboard_log=f"runs")
model.learn(
    total_timesteps=config["total_timesteps"],
    callback=WandbCallback(
        model_save_path=f"models/{run.id}",
        gradient_save_freq=100,
        log="all",
    ),
)
```
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from stable_baselines3.common.callbacks import BaseCallback  # type: ignore

import backprop as wandb


logger = logging.getLogger(__name__)


class WandbCallback(BaseCallback):
    """Callback for logging experiments to Weights and Biases.

    Log SB3 experiments to Weights and Biases
        - Added model tracking and uploading
        - Added complete hyperparameters recording
        - Added gradient logging
        - Note that `wandb.init(...)` must be called before the WandbCallback can be used.

    Args:
        verbose: The verbosity of sb3 output
        model_save_path: Path to the folder where the model will be saved, The default value is `None` so the model is not logged
        model_save_freq: Frequency to save the model
        gradient_save_freq: Frequency to log gradient. The default value is 0 so the gradients are not logged
        log: What to log. One of "gradients", "parameters", or "all".
    """

    def __init__(
        self,
        verbose: int = 0,
        model_save_path: str | None = None,
        model_save_freq: int = 0,
        gradient_save_freq: int = 0,
        log: Literal["gradients", "parameters", "all"] | None = "all",
    ) -> None:
        super().__init__(verbose)
        if wandb.run is None:
            raise wandb.Error("You must call wandb.init() before WandbCallback()")

        self.model_save_freq = model_save_freq
        self.model_save_path = model_save_path
        self.gradient_save_freq = gradient_save_freq
        self._backprop_watched = False
        if log not in ["gradients", "parameters", "all", None]:
            wandb.termwarn(
                "`log` must be one of `None`, 'gradients', 'parameters', or 'all', "
                "falling back to 'all'"
            )
            log = "all"
        self.log = log
        # Create folder if needed
        if self.model_save_path is not None:
            os.makedirs(self.model_save_path, exist_ok=True)
            self.path = os.path.join(self.model_save_path, "model.zip")
        else:
            assert self.model_save_freq == 0, (
                "to use the `model_save_freq` you have to set the `model_save_path` parameter"
            )

    def _init_callback(self) -> None:
        """Record model settings and install hooks for this training session.

        Main caller: BaseCallback.init_callback at the start of model.learn.
        Why this exists: a reused callback must release the model's observer
        hooks even when recording configuration raises an exception.
        """
        d = {}
        if "algo" not in d:
            d["algo"] = type(self.model).__name__
        for key in self.model.__dict__:
            if key in wandb.config:
                continue
            if type(self.model.__dict__[key]) in [float, int, str]:
                d[key] = self.model.__dict__[key]
            else:
                d[key] = str(self.model.__dict__[key])
        if self.gradient_save_freq > 0:
            wandb.watch(
                self.model.policy,
                log_freq=self.gradient_save_freq,
                log=self.log,
            )
            self._backprop_watched = self.log is not None
        try:
            wandb.config.setdefaults(d)
        except BaseException:
            self._stop_watching()
            raise

    def _on_step(self) -> bool:
        if (
            self.model_save_freq > 0
            and self.model_save_path is not None
            and self.n_calls % self.model_save_freq == 0
        ):
            self.save_model()
        return True

    def _on_training_end(self) -> None:
        """Save the final checkpoint and always release this session's hooks.

        Main caller: BaseCallback.on_training_end after successful learning.
        Saving can fail, so cleanup belongs to the finally block.
        """
        try:
            if self.model_save_path is not None:
                self.save_model()
        finally:
            self._stop_watching()

    def _stop_watching(self) -> None:
        """Remove hooks after a completed or failed checkpoint operation.

        Main callers: initialization rollback, save_model and training end.
        Why this exists: SB3 does not call on_training_end when an on_step
        checkpoint raises. The save boundary must therefore clean up itself.
        """
        if self._backprop_watched:
            wandb.unwatch(self.model.policy)
            self._backprop_watched = False

    def save_model(self) -> None:
        """Save and publish the real SB3 archive, preserving any save error.

        Main callers: periodic _on_step, final _on_training_end, and explicit
        caller checkpoints. Failure releases observer hooks before propagating
        the original exception, so another training session starts cleanly.
        """
        try:
            self.model.save(self.path)
            wandb.save(self.path, base_path=self.model_save_path)
        except BaseException:
            self._stop_watching()
            raise
        if self.verbose > 1:
            logger.info(f"Saving model checkpoint to {self.path}")
