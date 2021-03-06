#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function, division
try:
    from Tkinter import Tk, Toplevel, StringVar, N, E, S, W, LabelFrame, BOTH
    from ttk import Frame
    from tkMessageBox import showerror, askokcancel
    import cPickle as pickle
    pickle_opts = {}
    from Queue import Queue
except ImportError:
    from tkinter import Tk, Toplevel, StringVar, N, E, S, W, LabelFrame, BOTH
    from tkinter.ttk import Frame
    from tkinter.messagebox import showerror, askokcancel
    import pickle
    pickle_opts = {'encoding': 'latin1'}
    from queue import Queue
from epics import caget, caput, PV
from datetime import datetime
from threading import Thread

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.patches import Rectangle
from matplotlib.pyplot import Figure, rcdefaults, rcParams, close
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                               NavigationToolbar2TkAgg)
from numpy import (linspace, arange, concatenate, pi, complex128, zeros,
                   empty, angle, unwrap, diff, abs as npabs, ones, vstack, exp,
                   log, linalg, cos, polyfit, poly1d, sqrt, diag, save, load,
                   sum as npsum)
import pyfftw
from layout import (cs_label, cs_Strentry, cs_checkbox, cp_label, cs_button)
from hdf5 import h5load
from scipy.interpolate import InterpolatedUnivariateSpline
from scipy.optimize import curve_fit
from pandas import rolling_mean
from subprocess import Popen, PIPE
simd_alignment = pyfftw.simd_alignment


effort = ['FFTW_ESTIMATE', 'FFTW_MEASURE', 'FFTW_PATIENT', 'FFTW_EXHAUSTIVE']
def init_pyfftw(N):
    a = pyfftw.n_byte_align_empty(N, simd_alignment, 'complex128')
    b = pyfftw.n_byte_align_empty(N, simd_alignment, 'complex128')
    c = pyfftw.n_byte_align_empty(N, simd_alignment, 'complex128')
    fft = pyfftw.FFTW(a, b, threads=2, effort=effort[0])
    ifft = pyfftw.FFTW(b, c, direction='FFTW_BACKWARD', threads=2,
                       effort=effort[0])
    return fft, ifft


def noisefilter(t, signal, avgpts=30, smoothfac=1600):
    smooth = rolling_mean(signal[::-1], avgpts)[::-1]
    fspline = InterpolatedUnivariateSpline(t[:-avgpts], smooth[:-avgpts], k=4)
    fspline.set_smoothing_factor(smoothfac)
    return fspline(t)


def hanning(N):
    return (cos(linspace(-pi, pi, N))+1)/2


def drawpatch(ax, leftx, width):
    ylimits = ax.get_ylim()
    height = ylimits[1] - ylimits[0]
    patch = Rectangle((leftx, ylimits[0]), width, height, alpha=0.1, edgecolor="#ff0000", facecolor="#ff0000")
    ax.add_patch(patch)


def tswa(sig, cur, pars, calib, fitorder=1):
    N, t, turns, roisig, roidamp, fs, dt, myfftw, myifftw, fd = pars

    beg, end = roisig
    sig = sig[beg:end]

    bbfbcntsnorm = (sig.T/cur).T
    bbfbpos = calib(bbfbcntsnorm)

    # prepare frequency filter
    fcent, fsigm = 190, 50
    fleft, fright = fcent - fsigm, fcent + fsigm
    pts_lft = sum(fd < fleft)
    pts_rgt = sum(fd > fright)
    pts_roi = len(fd) - pts_lft - pts_rgt
    frequencyfilter = concatenate((zeros(pts_lft), hanning(pts_roi), zeros(pts_rgt+N/2)))

    # calculate fft of signal
    fftx = myfftw(bbfbpos)

    # clip negative frequencies
    fftx_clipped = fftx.copy()
    fftx_clipped[N/2+1:] = 0

    # restore lost energy of negative frequencies
    fftx_clipped[1:N/2] *= 2

    # apply frequency filter
    fftx_filtered = fftx_clipped*frequencyfilter

    # calculate inverse fft (analytical signal) of filtered and positive frequency only fft
    beg, end = roidamp
    t2 = linspace(0, t[-1], N-1)[beg:end]
    analytic_signal = myifftw(fftx_filtered)
    amplitude_envelope = npabs(analytic_signal)[:-1][beg:end]
    instantaneous_phase = (unwrap(angle(analytic_signal)))
    instantaneous_frequency = (diff(instantaneous_phase) / (2*pi) * fs /1e3)[beg:end]

    # Damping time
    expfun = lambda t, amp, tau: amp*exp(-t/tau)
    popt, pcov = curve_fit(expfun, t2, amplitude_envelope)
    errs = sqrt(diag(pcov))
    initialamp = [popt[0], errs[0]]
    tau = [popt[1], errs[1]]
    fdamp = expfun(t2, *popt)
    #chi2 = npsum((fdamp-amplitude_envelope)**2)
    #chi2_dof = chi2/(len(amplitude_envelope) - 1 - len(popt))
    fdamp2 = fdamp**2

    # Instantaneous frequency
    instfreq = noisefilter(t2, instantaneous_frequency, avgpts=30,
                           smoothfac=40000)

    # Amplitude dependant tune shift
    popt, pcov = polyfit(fdamp2, instfreq, fitorder, cov=True)
    errs = sqrt(diag(pcov))
    tswafit = poly1d(popt)(fdamp2)
    tswa = [popt[0]*1e3, errs[0]*1e3]

    plotdata = [turns, bbfbcntsnorm, t2, amplitude_envelope, fdamp,
                instantaneous_frequency, instfreq, fdamp2, tswafit]
    resultsdata = [initialamp, tau, tswa]
    return plotdata, resultsdata


def inittswa():
    fs = 1.2495*1e6           # sampling frequency in Hz
    dt = 1/fs

    roisig = [int(x.get()) for x in configs['roisig']]
    roidamp = [int(x.get()) for x in configs['roidamp']]
    beg, end = roisig
    N = end-beg                      # number of points taken per measurement
    turns = arange(N)
    t = turns*dt*1e3                 # time in ms
    fd = linspace(0, fs/2/1e3, N/2)  # create frequency vector

    # initialise pyfftw for both signals
    myfftw, myifftw = init_pyfftw(N)
    pars = N, t, turns, roisig, roidamp, fs, dt, myfftw, myifftw, fd
    return pars


data = h5load('guitswatestfile', False)
def tswaloop(q, configs, pv):
    # Turn on the cache for optimum pyfftw performance
    pyfftw.interfaces.cache.enable()

    # Import Wisdom Files for pyFFTW
    try:
        pyfftw.import_wisdom(load('wisdom.npy'))
        print('wisdom loaded from file')
    except:
        pass

    with open('calib_InterpolatedUnivariateSpline.pkl', 'rb') as fh:
        calib = pickle.load(fh, **pickle_opts)
 
    def cb(**kwargs):
        sig = kwargs['value']
        cur = pv.curt.get()
        global pars
        if not q['update_conf'].empty():
            print('updating...')
            q['update_conf'].get()
            pars = inittswa()
        try:
            plotdata, resultsdata  = tswa(sig, cur, pars, calib)
        except Exception as e:
            showerror(title='analysis error', message=e.message)
            q['run'].put(False)
            
        q['update_plots'].put(plotdata)
        root.event_generate('<<update_plots>>', when='tail')
        q['update_results'].put(resultsdata)
        root.event_generate('<<update_results>>', when='tail')
        
        if not q['run'].empty():
            cbvar.clear_callbacks()
            save('wisdom.npy', pyfftw.export_wisdom())
            print('wisdom saved')
    
    pars = 0
    cbvar = PV('BBQR:X:SB:RAW', auto_monitor=True, callback=cb)
        

#    # activate callback for BBQR:X:SB:RAW
#    while q['run'].empty():
#        sig, cur = data['BBQR:X:SB:RAW'], data['TOPUPCC:rdCurCS']
#        if not q['update_conf'].empty():
#            print('updating...')
#            q['update_conf'].get()
#            pars = inittswa()
#
#        try:
#            plotdata, res_amp, res_tau, res_tswa  = tswa(sig, cur, pars, calib)
#        except Exception as e:
#            showerror(title='analysis error', message=e.message)
#            q['run'].put(False)
#
#        resultsdata = [res_amp, res_tau, res_tswa]
#        if epics['write_amp'].get():
#            caput(epics['amp'], res_amp[0])
#        if epics['write_tau'].get():
#            caput(epics['tau'], res_tau[0])
#        if epics['write_tswa'].get():
#            caput(epics['tswa'], res_tswa[0])
#
#        q['update_plots'].put(plotdata)
#        root.event_generate('<<update_plots>>', when='tail')
#        q['update_results'].put(resultsdata)
#        root.event_generate('<<update_results>>', when='tail')
    return


def runthread(fun, argstuple):
    # data plotting in new thread to keep gui (main thread&loop) responsive
    t_run = Thread(target=fun, args=argstuple)
    # automatically let die with main thread -> no global stop required
    t_run.setDaemon(True)
    # start thread
    t_run.start()
    return


def initfigs(labelframe):
    close('all')
    # destroy all widgets in fram/tab and close all figures
    for widget in labelframe.winfo_children():
        widget.destroy()
    fig = Figure(frameon=False)
    axes = [fig.add_subplot(2, 2, i+1) for i in range(4)]
    xlabs = ['turns',
             'time / (ms)',
             'time / (ms))',
             r'square amplitude / $(mm^2)$']
    ylabs = ['ADC counts',
             'betatron amplitude / (mm)',
             'frequency / (kHz)',
             'frequency / (kHz)']
    stylesets = [['-b'],
                 ['-b', '-r'],
                 ['-b', '-r'],
                 ['-b', '-r']]
    lines = []
    for ax, xlab, ylab, styles in zip(axes, xlabs, ylabs, stylesets):
        lines.append([ax.plot([], [], style)[0] for style in styles])
        ax.set_xlabel(xlab)
        ax.set_ylabel(ylab)
        ax.grid(True)
    canvas = FigureCanvasTkAgg(fig, master=labelframe)
    canvas._tkcanvas.config(highlightthickness=0)
    toolbar = NavigationToolbar2TkAgg(canvas, labelframe)
    canvas.get_tk_widget().pack()
    toolbar.pack()
    return fig, axes, lines


if __name__ == '__main__':
    root = Tk()
    root.title('Tuneshift with ampltiude measurement')
    frame = Frame(root)
    frame.pack(fill=BOTH, expand=True)
    lf_control = LabelFrame(frame, text="Control", padx=5, pady=5)
    lf_control.grid(row=0, column=0, sticky=W+E+N+S, padx=10, pady=10)
    lf_settings = LabelFrame(frame, text="Settings", padx=5, pady=5)
    lf_settings.grid(row=1, column=0, sticky=W+E+N+S, padx=10, pady=10)
    lf_results = LabelFrame(frame, text="Results", padx=5, pady=5)
    lf_results.grid(row=2, column=0, sticky=W+E+N+S, padx=10, pady=10)
    lf_plots = LabelFrame(frame, text="Matplotlib", padx=5, pady=5)
    lf_plots.grid(row=0, column=1, rowspan=3, sticky=W+E+N+S, padx=10, pady=10)

    rcdefaults()
    params = {'axes.labelsize': 10,
              'axes.titlesize': 10,
              'axes.formatter.limits': [-2, 3],
              'axes.grid': True,
              'figure.figsize': [8, 4.5],
              'figure.dpi': 100,
              'figure.autolayout': True,
              'figure.frameon': False,
              'font.size': 10,
              'font.family': 'serif',
              'legend.fontsize': 10,
              'lines.markersize': 4,
              'lines.linewidth': 1,
              'savefig.dpi': 100,
              'savefig.facecolor': 'white',
              'savefig.edgecolor': 'white',
              'savefig.format': 'pdf',
              'savefig.bbox': 'tight',
              'savefig.pad_inches': 0.05,
              'text.usetex': True,
              'xtick.labelsize': 10,
              'ytick.labelsize': 10}
    rcParams.update(params)
    fig, axes, lines = initfigs(lf_plots)

    q = {}
    q['run'] = Queue()
    q['run'].put(False)
    def _start():
        if not q['run'].empty():
            q['run'].get()
            # start actual program in thread
            q['update_conf'].put(1)
            runthread(tswaloop, (q, configs, pv))
        else:
            showerror(title='user error', message='already running!')
        return
    cs_button(lf_control, 1, 1, 'Start', _start)

    def _stop():
        q['run'].put(False)
        return
    cs_button(lf_control, 1, 2, 'Stop', _stop)

    def _update():
        q['update_conf'].put(1)
        return

    configs = {}
    configs['roisig'] = []
    configs['roidamp'] = []
    cs_label(lf_settings, 0, 0, 'ROI signal', grid_conf={'columnspan' : '3', 'sticky' : 'W'})
    configs['roisig'].append(cs_Strentry(lf_settings, 1, 0, '38460', entry_conf={'width' : '6'}))
    cs_label(lf_settings, 1, 1, '-')
    configs['roisig'].append(cs_Strentry(lf_settings, 1, 2, '87440', entry_conf={'width' : '6'}))
    cs_label(lf_settings, 2, 0, 'ROI betatron amplitude', grid_conf={'columnspan' : '3', 'sticky' : 'W'})
    configs['roidamp'].append(cs_Strentry(lf_settings, 3, 0, '23', entry_conf={'width' : '6'}))
    cs_label(lf_settings, 3, 1, '-')
    configs['roidamp'].append(cs_Strentry(lf_settings, 3, 2, '6000', entry_conf={'width' : '6'}))
    cs_button(lf_settings, 9, 0, 'Apply', _update)

    results, lab = {}, {}
    cs_label(lf_results, 0, 0, 'Initial amplitude', grid_conf={'sticky' : 'W'})
    results['amp'], lab['amp'] = cs_label(lf_results, 1, 0, 'nan',
                                          label_conf={'fg' : 'red'},
                                          grid_conf={'sticky' : 'W'})

    cs_label(lf_results, 2, 0, 'Damping time', grid_conf={'sticky' : 'W'})
    results['tau'], lab['tau'] = cs_label(lf_results, 3, 0, 'nan',
                                          label_conf={'fg' : 'blue'},
                                          grid_conf={'sticky' : 'W'})

    cs_label(lf_results, 4, 0, 'Tune Shift With Amplitude', grid_conf={'sticky' : 'W'})
    results['tswa'], lab['tswa'] = cs_label(lf_results, 5, 0, 'nan',
                                            label_conf={'fg' : 'green'},
                                            grid_conf={'sticky' : 'W'})

    cs_label(lf_results, 6, 0, 'Measurement Nr.', grid_conf={'sticky' : 'W'})
    results['pnt'] = cs_label(lf_results, 7, 0, '0',
                              grid_conf={'sticky' : 'W'})[0]

    epics = {}
    cs_label(lf_results, 0, 1, 'Write')
    epics['write_amp'] = cs_checkbox(lf_results, 1, 1, '', False)
    epics['amp'] = 'FKC00V'
    epics['write_tau'] = cs_checkbox(lf_results, 3, 1, '', False)
    epics['tau'] = 'FKC01V'
    epics['write_tswa'] = cs_checkbox(lf_results, 5, 1, '', False)
    epics['tswa'] = 'FKC02V'
    
    class pv:
        fill = PV('CUMZR:MBcurrent')
        curt = PV('TOPUPCC:rdCurCS')
        ampl = PV(epics['amp'])
        tau = PV(epics['tau'])
        tswa = PV(epics['tswa'])

    popup = Toplevel(bg='')
    popup.overrideredirect(True)
    popup.withdraw()
    popupstr, popuplab = cp_label(popup, '',
                                  label_conf={'bg':'black', 'fg':'green',
                                              'font':'Mono 15'},
                                  pack_conf={'side':'right'})

    def b2click(event, text):
        # get epics var name
        text = epics[text]

        # create popup displaying epics name
        popupstr.set(text)
        popup.update()
        w, h = popuplab.winfo_width() + 15, popuplab.winfo_height()
        x, y = root.winfo_pointerx(), root.winfo_pointery()
        popup.wm_geometry('{:d}x{:d}+{:d}+{:d}'.format(w, h, x, y))
        popup.deiconify()

        # copy epics variable to xsel
        # universal_newlines -> input and output as text (not byte) strings
        p = Popen(['xsel', '-pi'], stdin=PIPE, universal_newlines=True)
        p.communicate(input=text)

        # copy epics name to clipboard
        root.clipboard_clear()
        root.clipboard_append(text)
        return
    [lab[l].bind("<Button-2>", lambda event, text=l: b2click(event, text)) for l in lab]
    [lab[l].bind("<ButtonRelease-2>", lambda event: popup.withdraw()) for l in lab]

    # define threadsafe event handling functions that get required data from q
    q['update_conf'] = Queue()
    q['update_plots'] = Queue()
    def update_plots(event):
        plotdata = q['update_plots'].get()
        [turns, bbfbcntsnorm, t2, amplit, fdamp, signal, instfreq, fdamp2,
         tswafit] = plotdata
        lines[0][0].set_data(turns, bbfbcntsnorm)

        lines[1][0].set_data(t2, amplit)
        lines[1][1].set_data(t2, fdamp)

        lines[2][0].set_data(t2, signal)
        lines[2][1].set_data(t2, instfreq)

        lines[3][0].set_data(fdamp2, instfreq)
        lines[3][1].set_data(fdamp2, tswafit)

        for ax in axes:
            ax.relim()
            ax.autoscale_view(tight=True, scalex=True, scaley=True)

        fig.canvas.draw()
        return
    root.bind('<<update_plots>>', update_plots)

    q['update_results'] = Queue()
    def update_results(event):
        resultsdata = q['update_results'].get()
        res_amp, res_tau, res_tswa = resultsdata
        if res_tau[1]/res_tau[0] < 0.05:
            if epics['write_amp'].get():
                pv.ampl.put(res_amp[0])
            if epics['write_tau'].get():
                pv.tau.put(res_tau[0])
            if epics['write_tswa'].get():
                pv.tswa.put(res_tswa[0])
            results['amp'].set(('{0:.2f} ' + u'\u00B1' + ' {1:.2f} mm').format(*res_amp))
            results['tau'].set(('{0:.2f} ' + u'\u00B1' + ' {1:.2f} ms').format(*res_tau))
            results['tswa'].set(('{0:.2f} ' + u'\u00B1' + ' {1:.2f} Hz/mm' + u'\u00B2').format(*res_tswa))
            results['pnt'].set(int(results['pnt'].get()) + 1)
        else:
            results['amp'].set('error > 10%')
            results['tau'].set('error > 10%')
            results['tswa'].set('error > 10%')
            results['pnt'].set(int(results['pnt'].get()) + 1)
        return
    root.bind('<<update_results>>', update_results)

    # take care of threadsafe quit
    def quitgui():
        if askokcancel("Quit", "Do you want to quit?"):
            q['run'].put(False)
            root.after(2000)
            root.destroy()
            root.quit()
    root.protocol("WM_DELETE_WINDOW", quitgui)
    root.mainloop()