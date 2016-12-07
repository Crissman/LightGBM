# coding: utf-8
# pylint: disable = invalid-name, W0105
"""Training Library containing training routines of LightGBM."""
from __future__ import absolute_import

import collections
from operator import attrgetter
import numpy as np
from .basic import LightGBMError, Predictor, Dataset, Booster, is_str
from . import callback

def _construct_dataset(X_y, reference=None,
                       params=None, other_fields=None,
                       feature_name=None, categorical_feature=None,
                       predictor=None):
    if 'max_bin' in params:
        max_bin = int(params['max_bin'])
    else:
        max_bin = 255
    weight = None
    group = None
    init_score = None
    if other_fields is not None:
        if not isinstance(other_fields, dict):
            raise TypeError("type of other filed data should be dict")
        weight = other_fields.get('weight', None)
        group = other_fields.get('group', None)
        init_score = other_fields.get('init_score', None)
    if is_str(X_y):
        data = X_y
        label = None
    else:
        if len(X_y) != 2:
            raise TypeError("should pass (data, label) tuple for dataset")
        data = X_y[0]
        label = X_y[1]
    if reference is None:
        ret = Dataset(data, label=label, max_bin=max_bin,
                      weight=weight, group=group,
                      predictor=predictor,
                      feature_name=feature_name,
                      categorical_feature=categorical_feature,
                      params=params)
    else:
        ret = reference.create_valid(data, label=label, weight=weight,
                                     group=group, params=params)
    if init_score is not None:
        ret.set_init_score(init_score)
    return ret

def train(params, train_data, num_boost_round=100,
          valid_datas=None, valid_names=None,
          fobj=None, feval=None, init_model=None,
          train_fields=None, valid_fields=None,
          feature_name=None, categorical_feature=None,
          early_stopping_rounds=None, evals_result=None,
          verbose_eval=True, learning_rates=None, callbacks=None):
    """Train with given parameters.

    Parameters
    ----------
    params : dict
        Parameters for training.
    train_data : Dataset, tuple (X, y) or filename of data
        Data to be trained.
    num_boost_round: int
        Number of boosting iterations.
    valid_datas: list of Datasets, tuples (valid_X, valid_y) or filenames of data
        List of data to be evaluated during training
    valid_names: list of string
        Names of valid_datas
    fobj : function
        Customized objective function.
    feval : function
        Customized evaluation function.
        Note: should return (eval_name, eval_result, is_higher_better) of list of this
    init_model : file name of lightgbm model or 'Booster' instance
        model used for continued train
    train_fields : dict
        Other data file in training data. e.g. train_fields['weight'] is weight data
        Support fields: weight, group, init_score
    valid_fields : dict
        Other data file in training data. \
        e.g. valid_fields[0]['weight'] is weight data for first valid data
        Support fields: weight, group, init_score
    feature_name : list of str
        Feature names
    categorical_feature : list of str or int
        Categorical features, type int represents index, \
        type str represents feature names (need to specify feature_name as well)
    early_stopping_rounds: int
        Activates early stopping.
        Requires at least one validation data and one metric
        If there's more than one, will check all of them
        Returns the model with (best_iter + early_stopping_rounds)
        If early stopping occurs, the model will add 'best_iteration' field
    evals_result: dict or None
        This dictionary used to store all evaluation results of all the items in valid_datas.
        Example: with a valid_datas containing [valid_set, train_set] \
        and valid_names containing ['eval', 'train'] and a paramater containing ('metric':'logloss')
        Returns: {'train': {'logloss': ['0.48253', '0.35953', ...]},
                  'eval': {'logloss': ['0.480385', '0.357756', ...]}}
        passed with None means no using this function
    verbose_eval : bool or int
        Requires at least one item in evals.
        If `verbose_eval` is True then the evaluation metric on the validation set is \
        printed at each boosting stage.
        If `verbose_eval` is an integer then the evaluation metric on the validation set \
        is printed at every given `verbose_eval` boosting stage. The last boosting stage \
        / the boosting stage found by using `early_stopping_rounds` is also printed.
        Example: with verbose_eval=4 and at least one item in evals, an evaluation metric \
        is printed every 4 boosting stages, instead of every boosting stage.
    learning_rates: list or function
        List of learning rate for each boosting round \
        or a customized function that calculates learning_rate in terms of \
        current number of round and the total number of boosting round \
        (e.g. yields learning rate decay)
        - list l: learning_rate = l[current_round]
        - function f: learning_rate = f(current_round, total_boost_round) \
        or learning_rate = f(current_round)
    callbacks : list of callback functions
        List of callback functions that are applied at end of each iteration.

    Returns
    -------
    booster : a trained booster model
    """
    """create predictor first"""
    if is_str(init_model):
        predictor = Predictor(model_file=init_model)
    elif isinstance(init_model, Booster):
        predictor = init_model.to_predictor()
    elif isinstance(init_model, Predictor):
        predictor = init_model
    else:
        predictor = None
    init_iteration = predictor.num_total_iteration if predictor else 0
    """create dataset"""
    if isinstance(train_data, Dataset):
        train_set = train_data
        if train_fields is not None:
            for field, data in train_fields.items():
                train_set.set_field(field, data)
    else:
        train_set = _construct_dataset(train_data, None, params,
                                       other_fields=train_fields,
                                       feature_name=feature_name,
                                       categorical_feature=categorical_feature,
                                       predictor=predictor)
    is_valid_contain_train = False
    train_data_name = "training"
    valid_sets = []
    name_valid_sets = []
    if valid_datas:
        if isinstance(valid_datas, (Dataset, tuple)):
            valid_datas = [valid_datas]
        if isinstance(valid_names, str):
            valid_names = [valid_names]
        for i, valid_data in enumerate(valid_datas):
            other_fields = None if valid_fields is None else valid_fields.get(i, None)
            """reduce cost for prediction training data"""
            if valid_data[0] is train_data[0] and valid_data[1] is train_data[1]:
                is_valid_contain_train = True
                if valid_names is not None:
                    train_data_name = valid_names[i]
                continue
            if isinstance(valid_data, Dataset):
                valid_set = valid_data
                if other_fields is not None:
                    for field, data in other_fields.items():
                        valid_set.set_field(field, data)
            else:
                valid_set = _construct_dataset(
                    valid_data,
                    train_set,
                    params,
                    other_fields=other_fields,
                    feature_name=feature_name,
                    categorical_feature=categorical_feature,
                    predictor=predictor)
            valid_sets.append(valid_set)
            if valid_names is not None and len(valid_names) > i:
                name_valid_sets.append(valid_names[i])
            else:
                name_valid_sets.append('valid_'+str(i))

    """process callbacks"""
    if not callbacks:
        callbacks = set()
    else:
        for i, cb in enumerate(callbacks):
            cb.__dict__.setdefault('order', i - len(callbacks))
        callbacks = set(callbacks)

    # Most of legacy advanced options becomes callbacks
    if verbose_eval is True:
        callbacks.add(callback.print_evaluation())
    elif isinstance(verbose_eval, int):
        callbacks.add(callback.print_evaluation(verbose_eval))

    if early_stopping_rounds:
        callbacks.add(callback.early_stop(early_stopping_rounds,
                                          verbose=bool(verbose_eval)))

    if learning_rates is not None:
        callbacks.add(callback.reset_learning_rate(learning_rates))

    if evals_result is not None:
        callbacks.add(callback.record_evaluation(evals_result))

    callbacks_before_iter = {cb for cb in callbacks if getattr(cb, 'before_iteration', False)}
    callbacks_after_iter = callbacks - callbacks_before_iter
    callbacks_before_iter = sorted(callbacks_before_iter, key=attrgetter('order'))
    callbacks_after_iter = sorted(callbacks_after_iter, key=attrgetter('order'))

    """construct booster"""
    booster = Booster(params=params, train_set=train_set)
    if is_valid_contain_train:
        booster.set_train_data_name(train_data_name)
    for valid_set, name_valid_set in zip(valid_sets, name_valid_sets):
        booster.add_valid(valid_set, name_valid_set)

    """start training"""
    for i in range(init_iteration, init_iteration + num_boost_round):
        for cb in callbacks_before_iter:
            cb(callback.CallbackEnv(model=booster,
                                    cvfolds=None,
                                    iteration=i,
                                    begin_iteration=init_iteration,
                                    end_iteration=init_iteration + num_boost_round,
                                    evaluation_result_list=None))

        booster.update(fobj=fobj)

        evaluation_result_list = []
        # check evaluation result.
        if valid_sets:
            if is_valid_contain_train:
                evaluation_result_list.extend(booster.eval_train(feval))
            evaluation_result_list.extend(booster.eval_valid(feval))
        try:
            for cb in callbacks_after_iter:
                cb(callback.CallbackEnv(model=booster,
                                        cvfolds=None,
                                        iteration=i,
                                        begin_iteration=init_iteration,
                                        end_iteration=init_iteration + num_boost_round,
                                        evaluation_result_list=evaluation_result_list))
        except callback.EarlyStopException:
            break
    if booster.attr('best_iteration') is not None:
        booster.best_iteration = int(booster.attr('best_iteration')) + 1
    else:
        booster.best_iteration = num_boost_round
    return booster


class CVBooster(object):
    """"Auxiliary datastruct to hold one fold of CV."""
    def __init__(self, train_set, valid_test, params):
        """"Initialize the CVBooster"""
        self.train_set = train_set
        self.valid_test = valid_test
        self.booster = Booster(params=params, train_set=train_set)
        self.booster.add_valid(valid_test, 'valid')

    def update(self, fobj):
        """"Update the boosters for one iteration"""
        self.booster.update(fobj=fobj)

    def eval(self, feval):
        """"Evaluate the CVBooster for one iteration."""
        return self.booster.eval_valid(feval)

try:
    from sklearn.model_selection import StratifiedKFold
    SKLEARN_StratifiedKFold = True
except ImportError:
    try:
        from sklearn.cross_validation import StratifiedKFold
        SKLEARN_StratifiedKFold = True
    except ImportError:
        SKLEARN_StratifiedKFold = False

def _make_n_folds(full_data, nfold, params, seed, fpreproc=None, stratified=False):
    """
    Make an n-fold list of CVBooster from random indices.
    """
    np.random.seed(seed)
    if stratified:
        if SKLEARN_StratifiedKFold:
            sfk = StratifiedKFold(n_splits=nfold, shuffle=True, random_state=seed)
            idset = [x[1] for x in sfk.split(X=full_data.get_label(), y=full_data.get_label())]
        else:
            raise LightGBMError('sklearn needs to be installed in order to use stratified cv')
    else:
        randidx = np.random.permutation(full_data.num_data())
        kstep = int(len(randidx) / nfold)
        idset = [randidx[(i * kstep): min(len(randidx), (i + 1) * kstep)] for i in range(nfold)]

    ret = []
    for k in range(nfold):
        train_set = full_data.subset(np.concatenate([idset[i] for i in range(nfold) if k != i]))
        valid_set = full_data.subset(idset[k])
        # run preprocessing on the data set if needed
        if fpreproc is not None:
            train_set, valid_set, tparam = fpreproc(train_set, valid_set, params.copy())
        else:
            tparam = params
        ret.append(CVBooster(train_set, valid_set, tparam))
    return ret

def _agg_cv_result(raw_results):
    """
    Aggregate cross-validation results.
    """
    cvmap = collections.defaultdict(list)
    metric_type = {}
    for one_result in raw_results:
        for one_line in one_result:
            metric_type[one_line[1]] = one_line[3]
            cvmap[one_line[1]].append(one_line[2])
    return [('cv_agg', k, np.mean(v), metric_type[k], np.std(v)) for k, v in cvmap.items()]

def cv(params, train_data, num_boost_round=10, nfold=5, stratified=False,
       metrics=(), fobj=None, feval=None, train_fields=None,
       feature_name=None, categorical_feature=None,
       early_stopping_rounds=None, fpreproc=None,
       verbose_eval=None, show_stdv=True, seed=0,
       callbacks=None):
    """Cross-validation with given paramaters.

    Parameters
    ----------
    params : dict
        Booster params.
    train_data : tuple (X, y) or filename of data
        Data to be trained.
    num_boost_round : int
        Number of boosting iterations.
    nfold : int
        Number of folds in CV.
    stratified : bool
        Perform stratified sampling.
    folds : a KFold or StratifiedKFold instance
        Sklearn KFolds or StratifiedKFolds.
    metrics : string or list of strings
        Evaluation metrics to be watched in CV.
    fobj : function
        Custom objective function.
    feval : function
        Custom evaluation function.
    train_fields : dict
        Other data file in training data. e.g. train_fields['weight'] is weight data
        Support fields: weight, group, init_score
    feature_name : list of str
        Feature names
    categorical_feature : list of str or int
        Categorical features, type int represents index, \
        type str represents feature names (need to specify feature_name as well)
    early_stopping_rounds: int
        Activates early stopping. CV error needs to decrease at least \
        every <early_stopping_rounds> round(s) to continue.
        Last entry in evaluation history is the one from best iteration.
    fpreproc : function
        Preprocessing function that takes (dtrain, dtest, param) and returns \
        transformed versions of those.
    verbose_eval : bool, int, or None, default None
        Whether to display the progress. If None, progress will be displayed \
        when np.ndarray is returned. If True, progress will be displayed at \
        boosting stage. If an integer is given, progress will be displayed \
        at every given `verbose_eval` boosting stage.
    show_stdv : bool, default True
        Whether to display the standard deviation in progress.
        Results are not affected, and always contains std.
    seed : int
        Seed used to generate the folds (passed to numpy.random.seed).
    callbacks : list of callback functions
        List of callback functions that are applied at end of each iteration.

    Returns
    -------
    evaluation history : list(string)
    """
    if metrics:
        params.setdefault('metric', [])
        if is_str(metrics):
            params['metric'].append(metrics)
        else:
            params['metric'].extend(metrics)

    train_set = _construct_dataset(train_data, None, params,
								   other_fields=train_fields,
                                   feature_name=feature_name,
                                   categorical_feature=categorical_feature)

    results = collections.defaultdict(list)
    cvfolds = _make_n_folds(train_set, nfold, params, seed, fpreproc, stratified)

    # setup callbacks
    if not callbacks:
        callbacks = set()
    else:
        for i, cb in enumerate(callbacks):
            cb.__dict__.setdefault('order', i - len(callbacks))
        callbacks = set(callbacks)
    if early_stopping_rounds:
        callbacks.add(callback.early_stop(early_stopping_rounds, verbose=False))
    if verbose_eval is True:
        callbacks.add(callback.print_evaluation(show_stdv=show_stdv))
    elif isinstance(verbose_eval, int):
        callbacks.add(callback.print_evaluation(verbose_eval, show_stdv=show_stdv))

    callbacks_before_iter = {cb for cb in callbacks if getattr(cb, 'before_iteration', False)}
    callbacks_after_iter = callbacks - callbacks_before_iter
    callbacks_before_iter = sorted(callbacks_before_iter, key=attrgetter('order'))
    callbacks_after_iter = sorted(callbacks_after_iter, key=attrgetter('order'))

    for i in range(num_boost_round):
        for cb in callbacks_before_iter:
            cb(callback.CallbackEnv(model=None,
                                    cvfolds=cvfolds,
                                    iteration=i,
                                    begin_iteration=0,
                                    end_iteration=num_boost_round,
                                    evaluation_result_list=None))
        for fold in cvfolds:
            fold.update(fobj)
        res = _agg_cv_result([f.eval(feval) for f in cvfolds])
        for _, key, mean, _, std in res:
            results[key + '-mean'].append(mean)
            results[key + '-stdv'].append(std)
        try:
            for cb in callbacks_after_iter:
                cb(callback.CallbackEnv(model=None,
                                        cvfolds=cvfolds,
                                        iteration=i,
                                        begin_iteration=0,
                                        end_iteration=num_boost_round,
                                        evaluation_result_list=res))
        except callback.EarlyStopException as e:
            for k in results:
                results[k] = results[k][:e.best_iteration + 1]
            break
    return dict(results)