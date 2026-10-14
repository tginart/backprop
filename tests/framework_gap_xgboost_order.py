"""Give genuine XGBoost callbacks a declared order for paired observations.

Architecture: real logger and EarlyStopping -> one TrainingCallback delegate ->
real XGBoost training -> complete ordered lifecycle and boolean return trace.
Main caller: framework_gap_probe.xgboost_cases.
Why this exists: XGBoost 2.0.3 CallbackContainer stores callback objects in a set
and short-circuits after a stopping callback returns True. Separate processes
can therefore invoke the logger on different final rounds. Both possible
orders are exercised without changing a callback or filtering logged calls.
"""


def ordered_callbacks(xgb, callbacks, dispatch):
    """Compose real callbacks and preserve XGBoost's stop-on-True boundary.

    Main caller: the pinned/native early stopping training probe. Training
    stages invoke every callback and carry its actual model return onward;
    iteration stages stop at the first actual True. The trace records every
    invocation, its returned model identity or boolean, and its actual epoch.
    """
    class OrderedCallbacks(xgb.callback.TrainingCallback):
        def _training(self, stage, model):
            for name, callback in callbacks:
                returned = getattr(callback, stage)(model)
                dispatch.append([stage, name, returned is model])
                model = returned
            return model

        def _iteration(self, stage, model, epoch, evals_log):
            for name, callback in callbacks:
                returned = getattr(callback, stage)(model, epoch, evals_log)
                dispatch.append([stage, name, epoch, bool(returned)])
                if returned:
                    return True
            return False

        def before_training(self, model):
            return self._training('before_training', model)

        def after_training(self, model):
            return self._training('after_training', model)

        def before_iteration(self, model, epoch, evals_log):
            return self._iteration('before_iteration', model, epoch, evals_log)

        def after_iteration(self, model, epoch, evals_log):
            return self._iteration('after_iteration', model, epoch, evals_log)

    return OrderedCallbacks()
