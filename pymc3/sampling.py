from collections import defaultdict

from joblib import Parallel, delayed
from numpy.random import randint, seed
from numpy import shape, asarray

import pymc3 as pm
from .backends.base import merge_traces, BaseTrace, MultiTrace
from .backends.ndarray import NDArray
from .model import modelcontext, Point
from .step_methods import (NUTS, HamiltonianMC, Metropolis, BinaryMetropolis,
                           BinaryGibbsMetropolis, CategoricalGibbsMetropolis,
                           Slice, CompoundStep)
from tqdm import tqdm

import sys
sys.setrecursionlimit(10000)

__all__ = ['sample', 'iter_sample', 'sample_ppc']


def assign_step_methods(model, step=None, methods=(NUTS, HamiltonianMC, Metropolis,
                                                   BinaryMetropolis, BinaryGibbsMetropolis,
                                                   Slice, CategoricalGibbsMetropolis)):
    '''
    Assign model variables to appropriate step methods. Passing a specified
    model will auto-assign its constituent stochastic variables to step methods
    based on the characteristics of the variables. This function is intended to
    be called automatically from `sample()`, but may be called manually. Each
    step method passed should have a `competence()` method that returns an
    ordinal competence value corresponding to the variable passed to it. This
    value quantifies the appropriateness of the step method for sampling the
    variable.

    Parameters
    ----------

    model : Model object
        A fully-specified model object
    step : step function or vector of step functions
        One or more step functions that have been assigned to some subset of
        the model's parameters. Defaults to None (no assigned variables).
    methods : vector of step method classes
        The set of step methods from which the function may choose. Defaults
        to the main step methods provided by PyMC3.

    Returns
    -------
    List of step methods associated with the model's variables.
    '''

    steps = []
    assigned_vars = set()
    if step is not None:
        try:
            steps += list(step)
        except TypeError:
            steps.append(step)
        for step in steps:
            try:
                assigned_vars = assigned_vars.union(set(step.vars))
            except AttributeError:
                for method in step.methods:
                    assigned_vars = assigned_vars.union(set(method.vars))

    # Use competence classmethods to select step methods for remaining
    # variables
    selected_steps = defaultdict(list)
    for var in model.free_RVs:
        if var not in assigned_vars:
            selected = max(methods, key=lambda method: method._competence(var))
            if model.verbose:
                pm._log.info('Assigned {0} to {1}'.format(selected.__name__, var))
            selected_steps[selected].append(var)

    # Instantiate all selected step methods
    steps += [step(vars=selected_steps[step]) for step in selected_steps if selected_steps[step]]

    if len(steps) == 1:
        steps = steps[0]

    return steps


def sample(draws, step=None, start=None, trace=None, chain=0, njobs=1, tune=None,
           progressbar=True, model=None, random_seed=-1):
    """
    Draw a number of samples using the given step method.
    Multiple step methods supported via compound step method
    returns the amount of time taken.

    Parameters
    ----------

    draws : int
        The number of samples to draw.
    step : function or iterable of functions
        A step function or collection of functions. If no step methods are
        specified, or are partially specified, they will be assigned
        automatically (defaults to None).
    start : dict
        Starting point in parameter space (or partial point)
        Defaults to trace.point(-1)) if there is a trace provided and
        model.test_point if not (defaults to empty dict).
    trace : backend, list, or MultiTrace
        This should be a backend instance, a list of variables to track,
        or a MultiTrace object with past values. If a MultiTrace object
        is given, it must contain samples for the chain number `chain`.
        If None or a list of variables, the NDArray backend is used.
        Passing either "text" or "sqlite" is taken as a shortcut to set
        up the corresponding backend (with "mcmc" used as the base
        name).
    chain : int
        Chain number used to store sample in backend. If `njobs` is
        greater than one, chain numbers will start here.
    njobs : int
        Number of parallel jobs to start. If None, set to number of cpus
        in the system - 2.
    tune : int
        Number of iterations to tune, if applicable (defaults to None)
    progressbar : bool
        Whether or not to display a progress bar in the command line. The
        bar shows the percentage of completion, the sampling speed in
        samples per second (SPS), and the estimated remaining time until
        completion ("expected time of arrival"; ETA).
    model : Model (optional if in `with` context)
    random_seed : int or list of ints
        A list is accepted if more if `njobs` is greater than one.

    Returns
    -------
    MultiTrace object with access to sampling values
    """
    model = modelcontext(model)

    step = assign_step_methods(model, step)

    if njobs is None:
        import multiprocessing as mp
        njobs = max(mp.cpu_count() - 2, 1)

    sample_args = {'draws': draws,
                   'step': step,
                   'start': start,
                   'trace': trace,
                   'chain': chain,
                   'tune': tune,
                   'progressbar': progressbar,
                   'model': model,
                   'random_seed': random_seed}

    if njobs > 1:
        sample_func = _mp_sample
        sample_args['njobs'] = njobs
    else:
        sample_func = _sample

    return sample_func(**sample_args)


def _sample(draws, step=None, start=None, trace=None, chain=0, tune=None,
            progressbar=True, model=None, random_seed=-1):
    sampling = _iter_sample(draws, step, start, trace, chain,
                            tune, model, random_seed)
    if progressbar:
        sampling = tqdm(sampling, total=draws)
    try:
        for strace in sampling:
            pass
    except KeyboardInterrupt:
        strace.close()
    return MultiTrace([strace])


def iter_sample(draws, step, start=None, trace=None, chain=0, tune=None,
                model=None, random_seed=-1):
    """
    Generator that returns a trace on each iteration using the given
    step method.  Multiple step methods supported via compound step
    method returns the amount of time taken.


    Parameters
    ----------

    draws : int
        The number of samples to draw
    step : function
        Step function
    start : dict
        Starting point in parameter space (or partial point)
        Defaults to trace.point(-1)) if there is a trace provided and
        model.test_point if not (defaults to empty dict)
    trace : backend, list, or MultiTrace
        This should be a backend instance, a list of variables to track,
        or a MultiTrace object with past values. If a MultiTrace object
        is given, it must contain samples for the chain number `chain`.
        If None or a list of variables, the NDArray backend is used.
    chain : int
        Chain number used to store sample in backend. If `njobs` is
        greater than one, chain numbers will start here.
    tune : int
        Number of iterations to tune, if applicable (defaults to None)
    model : Model (optional if in `with` context)
    random_seed : int or list of ints
        A list is accepted if more if `njobs` is greater than one.

    Example
    -------

    for trace in iter_sample(500, step):
        ...
    """
    sampling = _iter_sample(draws, step, start, trace, chain, tune,
                            model, random_seed)
    for i, strace in enumerate(sampling):
        yield MultiTrace([strace[:i + 1]])


def _iter_sample(draws, step, start=None, trace=None, chain=0, tune=None,
                 model=None, random_seed=-1):
    model = modelcontext(model)
    draws = int(draws)
    if random_seed != -1:
        seed(random_seed)
    if draws < 1:
        raise ValueError('Argument `draws` should be above 0.')

    if start is None:
        start = {}

    strace = _choose_backend(trace, chain, model=model)

    if len(strace) > 0:
        _soft_update(start, strace.point(-1))
    else:
        _soft_update(start, model.test_point)

    try:
        step = CompoundStep(step)
    except TypeError:
        pass

    point = Point(start, model=model)

    strace.setup(draws, chain)
    for i in range(draws):
        if i == tune:
            step = stop_tuning(step)
        point = step.step(point)
        strace.record(point)
        yield strace
    else:
        strace.close()


def _choose_backend(trace, chain, shortcuts=None, **kwds):
    if isinstance(trace, BaseTrace):
        return trace
    if isinstance(trace, MultiTrace):
        return trace._straces[chain]
    if trace is None:
        return NDArray(**kwds)

    if shortcuts is None:
        shortcuts = pm.backends._shortcuts

    try:
        backend = shortcuts[trace]['backend']
        name = shortcuts[trace]['name']
        return backend(name, **kwds)
    except TypeError:
        return NDArray(vars=trace, **kwds)
    except KeyError:
        raise ValueError('Argument `trace` is invalid.')


def _make_parallel(arg, njobs):
    if not shape(arg):
        return [arg] * njobs
    return arg


def _mp_sample(**kwargs):
    njobs = kwargs.pop('njobs')
    chain = kwargs.pop('chain')
    random_seed = kwargs.pop('random_seed')
    start = kwargs.pop('start')

    rseed = _make_parallel(random_seed, njobs)
    start_vals = _make_parallel(start, njobs)

    chains = list(range(chain, chain + njobs))
    pbars = [kwargs.pop('progressbar')] + [False] * (njobs - 1)
    traces = Parallel(n_jobs=njobs)(delayed(_sample)(chain=chains[i],
                                                     progressbar=pbars[i],
                                                     random_seed=rseed[i],
                                                     start=start_vals[i],
                                                     **kwargs) for i in range(njobs))
    return merge_traces(traces)


def stop_tuning(step):
    """ stop tuning the current step method """

    if hasattr(step, 'tune'):
        step.tune = False

    elif hasattr(step, 'methods'):
        step.methods = [stop_tuning(s) for s in step.methods]

    return step


def _soft_update(a, b):
    """As opposed to dict.update, don't overwrite keys if present.
    """
    a.update({k: v for k, v in b.items() if k not in a})


def sample_ppc(trace, samples=None, model=None, vars=None, size=None, random_seed=None, progressbar=True):
    """Generate posterior predictive samples from a model given a trace.

    Parameters
    ----------
    trace : backend, list, or MultiTrace
        Trace generated from MCMC sampling
    samples : int
        Number of posterior predictive samples to generate. Defaults to the
        length of `trace`
    model : Model (optional if in `with` context)
        Model used to generate `trace`
    vars : iterable
        Variables for which to compute the posterior predictive samples.
        Defaults to `model.observed_RVs`.
    size : int
        The number of random draws from the distribution specified by the
        parameters in each sample of the trace.

    Returns
    -------
    Dictionary keyed by `vars`, where the values are the corresponding
    posterior predictive samples.
    """
    if samples is None:
        samples = len(trace)

    if model is None:
        model = modelcontext(model)

    if vars is None:
        vars = model.observed_RVs

    seed(random_seed)

    if progressbar:
        indices = tqdm(randint(0, len(trace), samples), total=samples)
    else:
        indices = randint(0, len(trace), samples)

    ppc = defaultdict(list)
    for idx in indices:
        param = trace[idx]
        for var in vars:
            ppc[var.name].append(var.distribution.random(point=param,
                                                         size=size))

    return {k: asarray(v) for k, v in ppc.items()}
