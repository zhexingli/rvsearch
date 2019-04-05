import copy
import pdb

import numpy as np
import matplotlib.pyplot as plt
import astropy.stats
import radvel
import radvel.fitting
from radvel.plot import orbit_plots
from tqdm import tqdm
import pathos.multiprocessing as mp

import rvsearch.utils as utils


class Periodogram(object):
    """Class to calculate and store periodograms.

    Args:
        post (radvel.Posterior): radvel.Posterior object
        minsearchp (float): minimum search period
        maxsearchp (float): maximum search period
        baseline (bool): Whether to calculate maxsearchp from obs. baseline
        basefactor (float): How far past the obs. baseline to search
        oversampling (float): By how much to oversample the period grid
        manual_grid (array): Option to feed in a user-chosen period grid
        fap (float): False-alarm-probability threshold for detection
        num_pers (int): (optional) number of frequencies to test, default
        eccentric (bool): Whether to fit with free or fixed eccentricity
        workers (int): Number of cpus over which to parallelize
        verbose (bool): Whether to print progress during calculation

    """

    def __init__(self, post, basebic=None, minsearchp=3, maxsearchp=10000,
                 baseline=True, basefactor=5., oversampling=1., manual_grid=None,
                 fap=0.01, num_pers=None, eccentric=False, workers=1,
                 verbose=True):
        self.post = copy.deepcopy(post)
        self.default_pdict = {}
        for k in post.params.keys():
            self.default_pdict[k] = self.post.params[k].value

        self.basebic = basebic
        self.num_known_planets = self.post.params.num_planets - 1

        self.times = self.post.likelihood.x
        self.vel = self.post.likelihood.y
        self.errvel = self.post.likelihood.yerr
        self.timelen = np.amax(self.times) - np.amin(self.times)

        self.tels = []
        for val in self.post.params.keys():
            if 'gamma_' in val:
                self.tels.append(val.split('_')[1])

        self.minsearchP = minsearchp
        self.maxsearchP = maxsearchp
        self.baseline = baseline
        self.basefactor = basefactor
        self.oversampling = oversampling
        self.manual_grid = manual_grid
        self.fap = fap
        self.num_pers = num_pers
        if self.manual_grid is not None:
            self.num_pers = len(manual_grid)

        self.eccentric = eccentric

        if self.baseline == True:
            self.maxsearchP = self.basefactor * self.timelen

        self.valid_types = ['bic', 'aic', 'ls']
        self.power = {key: None for key in self.valid_types}

        self.workers = workers
        self.verbose = verbose

        self.best_per = None
        self.best_bic = None

        self.bic_thresh = None
        # Pre-compute good-fit floor of the BIC periodogram.
        self.floor = -2*np.log(len(self.times))

        # Automatically generate a period grid upon initialization.
        self.make_per_grid()
    '''
    @classmethod
    def from_pandas(cls, data):
        params = utils.initialize_default_pars(instnames=data.tel)
        post = utils.initialize_post(data, params=params)
        return cls(post)

    @classmethod
    def from_csv(cls, filename):
        data = utils.read_from_csv(filename)
        params = utils.initialize_default_pars(instnames=data.tel)
        post = utils.initialize_post(data, params=params)
        return cls(post)
    '''
    def per_spacing(self, verbose=True):
        """Get the number of sampled frequencies and return a period grid.

        Condition for spacing: delta nu such that during the
        entire duration of observations, phase slip is no more than P/4

        Args:
            verbose (bool): (optional) print extra messages

        Returns:
            array: Array of test periods

        """
        fmin = 1. / self.maxsearchP
        fmax = 1. / self.minsearchP

        dnu       = 1. / (4. * self.timelen)
        num_freq  = int((fmax - fmin) / dnu + 1)
        num_freq *= self.oversampling
        num_freq  = int(num_freq)

        if verbose:
            print("Number of test periods:", num_freq)

        freqs = np.linspace(fmax, fmin, num_freq)
        pers = 1. / freqs

        self.num_pers = num_freq
        return pers

    def make_per_grid(self):
        """Generate a grid of periods for which to compute likelihoods.

        """
        if self.manual_grid is not None:
            self.pers = np.array(self.manual_grid)
        else:
            if self.num_pers is None:
                self.pers = self.per_spacing()
            else:
                self.pers = 1/np.linspace(1/self.maxsearchP, 1/self.minsearchP,
                                          self.num_pers)

        self.freqs = 1/self.pers

    def per_bic(self):
        """Compute delta-BIC periodogram. ADD: crit is BIC or AIC.

        """
        prvstr = str(self.post.params.num_planets-1)
        plstr = str(self.post.params.num_planets)
        if self.verbose:
            print("Calculating BIC periodogram for {} planets vs. {} planets".format(plstr, prvstr))
        # This assumes nth planet parameters, and all periods, are fixed.
        if self.basebic is None:
            self.post.params['per'+plstr].vary = False
            self.post.params['tc'+plstr].vary = False
            self.post.params['k'+plstr].vary = False
            # Vary ONLY gamma, jitter, dvdt, curv. All else fixed, and k=0
            baseline_fit = radvel.fitting.maxlike_fitting(self.post, verbose=False)
            baseline_bic = baseline_fit.likelihood.bic()
        else:
            baseline_bic = self.basebic

        rms = np.std(self.post.likelihood.residuals())
        self.default_pdict['k{}'.format(self.post.params.num_planets)] = rms

        # Allow amplitude and time offset to vary, fix period (and ecc. if asked.)
        self.post.params['per{}'.format(self.num_known_planets+1)].vary = False
        if self.eccentric == False:
            # If eccentric set to False, fix eccentricity to zero.
            self.post.params['secosw{}'.format(self.num_known_planets+1)].vary = False
            self.post.params['sesinw{}'.format(self.num_known_planets+1)].vary = False

        self.post.params['k{}'.format(self.num_known_planets+1)].vary  = True
        self.post.params['tc{}'.format(self.num_known_planets+1)].vary = True

        # Divide period grid into as many subgrids as there are parallel workers.
        self.sub_pers = np.array_split(self.pers, self.workers)

        if self.verbose:
            # Create a separate progress bar for each parallel worker.
            pbars = [tqdm(total = len(self.sub_pers[i]), position=i) for i in
                     np.arange(self.workers)]

        # Define a function to compute periodogram for a given grid section.
        def fit_period(n):
            post = copy.deepcopy(self.post)
            per_array = self.sub_pers[n]
            '''
            TO DECIDE: WRITE PER_ARRAY AS SUB_PERS COPY, OR REFERENCE SUB_PERS?
            '''
            fit_params = [{} for x in range(len(per_array))]
            bic = np.zeros_like(per_array)

            for i, per in enumerate(per_array):
                # Reset posterior parameters to default values.
                for k in self.default_pdict.keys():
                    post.params[k].value = self.default_pdict[k]

                #Set new period, and fit a circular orbit.
                perkey = 'per{}'.format(self.num_known_planets+1)
                post.params[perkey].value = per
                post = radvel.fitting.maxlike_fitting(post, verbose=False)
                bic[i] = baseline_bic - post.likelihood.bic()

                if bic[i] < self.floor - 1:
                    # If the fit is bad, reset k_n+1 = 0 and try again.
                    for k in self.default_pdict.keys():
                        post.params[k].value = self.default_pdict[k]
                    post.params[perkey].value = per
                    post.params['k{}'.format(post.params.num_planets)].value = 0
                    post = radvel.fitting.maxlike_fitting(post, verbose=False)
                    bic[i] = baseline_bic - post.likelihood.bic()

                if bic[i] < self.floor - 1:
                    # If the fit is still bad, reset tc to better value and try again.
                    for k in self.default_pdict.keys():
                        post.params[k].value = self.default_pdict[k]
                    veldiff = np.absolute(post.likelihood.y - np.mean(post.likelihood.y))
                    tc_new = self.times[np.argmin(veldiff)]
                    post.params['tc{}'.format(post.params.num_planets)].value = tc_new
                    post = radvel.fitting.maxlike_fitting(post, verbose=False)
                    bic[i] = baseline_bic - post.likelihood.bic()

                # Append the best-fit parameters to the period-iterated list.
                best_params = {}
                for k in post.params.keys():
                    best_params[k] = post.params[k].value
                fit_params[i] = best_params

                if self.verbose:
                    pbars[n].update(1)

            return [bic, fit_params]

        if self.workers == 1:
            # Call the periodogram loop on one core.
            self.bic, self.fit_params = fit_period(0)
        else:
            # Parallelize the loop over sections of the period grid.
            p = mp.Pool(processes=self.workers)
            output = p.map(fit_period, np.arange(self.workers))

            # Sort output.
            all_bics = []
            all_params = []
            for chunk in output:
                all_bics.append(chunk[0])
                all_params.append(chunk[1])
            self.bic = [y for x in all_bics for y in x]
            self.fit_params = [y for x in all_params for y in x]

        fit_index = np.argmax(self.bic)
        self.bestfit_params = self.fit_params[fit_index]
        self.best_bic = self.bic[fit_index]
        self.power['bic'] = self.bic

        if self.verbose:
            # Clean up the progress bars.
            for pbar in pbars:
                pbar.close()
            for i in np.arange(self.workers):
                print('')

    def ls(self):
        """Compute Lomb-Scargle periodogram with astropy.

        """
        #FOR TESTING
        print("Calculating Lomb-Scargle periodogram")
        periodogram = astropy.stats.LombScargle(self.times, self.vel,
                                                        self.errvel)
        power = periodogram.power(np.flip(self.freqs))
        self.power['ls'] = power

    def eFAP_thresh(self):
        """Calculate the threshold for significance based on BJ's empirical
            false-alarm-probability algorithm.

        """
        # select out intermediate values of BIC, median - 95%
        sBIC = np.sort(self.power['bic'])
        crop_BIC = sBIC[int(0.5*len(sBIC)):int(0.95*len(sBIC))]

        hist, edge = np.histogram(crop_BIC, bins=10)
        cent = (edge[1:]+edge[:-1])/2.
        norm = float(np.sum(hist))
        nhist = hist/norm
        loghist = np.log10(nhist)

        func = np.poly1d(np.polyfit(cent[np.isfinite(loghist)], \
                                loghist[np.isfinite(loghist)], 1))
        xmod = np.linspace(np.min(sBIC[np.isfinite(sBIC)]), \
                                    10.*np.max(sBIC), 10000)
        lfit = 10.**func(xmod)
        fap_min = 10.**func(sBIC[-1])*self.num_pers
        thresh = xmod[np.where(np.abs(lfit-self.fap/self.num_pers) ==
                        np.min(np.abs(lfit-self.fap/self.num_pers)))]
        self.bic_thresh = thresh[0]

    def save_per(self, ls=False):
        """Save BIC periodogram as csv.

        Args:
            ls (bool): Save Lomb-Scargle periodogram?

        """
        if ls==False:
            try:
                np.savetxt((self.pers, self.power['bic']), filename=\
                                                'BIC_periodogram.csv')
            except:
                print('Have not generated a delta-BIC periodogram.')
        else:
            try:
                np.savetxt((self.pers, self.power['ls']), filename=\
                                                'LS_periodogram.csv')
            except:
                print('Have not generated a Lomb-Scargle periodogram.')

    def plot_per(self, alias=True, floor=True, save=False):
        """Plot periodogram.

        Args:
            alias (bool): Plot year, month, day aliases?
            floor (bool): Set y-axis minimum according to likelihood limit?
            save (bool): Save plot to current directory?

        """
        # TO-DO: WORK IN AIC/BIC OPTION, INCLUDE IN PLOT TITLE
        peak = np.argmax(self.power['bic'])
        f_real = self.freqs[peak]

        fig, ax = plt.subplots()
        ax.plot(self.pers, self.power['bic'])
        ax.scatter(self.pers[peak], self.power['bic'][peak], label='{} days'\
                            .format(np.round(self.pers[peak], decimals=1)))

        # If DBIC threshold has been calculated, plot.
        if self.bic_thresh is not None:
            ax.axhline(self.bic_thresh, ls=':', c='y', label='{} FAP'\
                                                    .format(self.fap))
            upper = 1.1*max(np.amax(self.power['bic']), self.bic_thresh)
        else:
            upper = 1.1*np.amax(self.power['bic'])
            
        if floor:
            # Set periodogram plot floor according to circular-fit BIC min.
            lower = -2*np.log(len(self.times))
        else:
            lower = np.amin(self.power['bic'])

        ax.set_ylim([lower, upper])
        ax.set_xlim([self.pers[0], self.pers[-1]])

        if alias:
            # Plot sidereal day, lunation period, and sidereal year aliases.
            colors = ['r', 'b', 'g']
            alias = [0.997, 29.531, 365.256]
            for i in np.arange(3):
                f_ap = 1./alias[i] + f_real
                f_am = 1./alias[i] - f_real
                ax.axvline(1./f_am, linestyle='--', c=colors[i], alpha=0.5,
                                label='{} day alias'.format(np.round(alias[i],
                                decimals=1)))
                ax.axvline(1./f_ap, linestyle='--', c=colors[i], alpha=0.5)

        ax.legend(loc=0)
        ax.set_xscale('log')
        ax.set_xlabel('Period (days)')
        ax.set_ylabel(r'$\Delta$BIC')  # TO-DO: WORK IN AIC/BIC OPTION
        ax.set_title('Planet {} vs. planet {}'.format(self.num_known_planets+1,
                                                      self.num_known_planets))

        # Store figure as object attribute, make separate saving functionality?
        self.fig = fig
        if save:
            fig.savefig('dbic{}.pdf'.format(self.num_known_planets+1))
