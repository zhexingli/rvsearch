#Utilities for loading data, checking for known planets, etc.
import numpy as np
import pandas as pd
import radvel
import cpsutils
from cpsutils import io

'''
Functions for posterior modification (resetting parameters, intializing, etc.)
'''
def reset_params(post, default_pdict):
	#Reset post.params values to default values
    pass

'''
Series of functions for reading data from various sources into pandas dataframes.
'''
def read_from_csv(filename, verbose=True):
    data = pd.DataFrame.from_csv(filename)
    if 'tel' not in data.columns:
        if verbose:
            print('Telescope type not given, defaulting to HIRES.')
        data['tel'] = 'HIRES'
        #Question: DO WE NEED TO CONFIRM VALID TELESCOPE TYPE?
    return data

def read_from_arrs(t, mnvel, errvel, tel=None, verbose=True):
    data = pd.DataFrame()
    data['time'], data['mnvel'], data['errvel'] = t, mnvel, errvel
    if tel == None:
        if verbose:
            print('Telescope type not given, defaulting to HIRES.')
        data['tel'] = 'HIRES'
    else:
        data['tel'] = tel
    return data

def read_from_vst(filename, verbose=True):
    '''
    This reads .vst files generated by the CPS pipeline, which
    means that it is only relevant for HIRES data.
    '''

    b = cpsutils.io.read_vst(filename)
    data = pd.DataFrame()
    data['time'] = b.jd
    data['mnvel'] = b.mnvel
    data['errvel'] = b.errvel
    data['tel'] = 'HIRES'
    return data

def read_from_cadence(starname, verbose=True):
    pass