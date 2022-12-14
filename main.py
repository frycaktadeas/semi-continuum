import os.path

import tiffile
from tifffile import tifffile

from retention_curves import *
from matplotlib import pyplot as plt
from scipy.ndimage import zoom
from tqdm import tqdm
import plotly.express as px
import seaborn as sns
import math
import time
import json
import cv2

sns.set_theme()

USE_GPU = True

if USE_GPU:
    import cupy as np
else:
    import numpy as np

# Define constants
REALTIME = 20  # simulation time [s]
dL = 0.25 * 0.01  # size of the block [m]
dx_PAR = dL / 0.01  # discretization parameter [-]  ratio of dL and 0.01m
S0 = 0.01  # initial saturation [-]

X_SIZE = 0.10  # width of the medium in x-axis [m]
Y_SIZE = 0.10  # width of the medium in y-axis [m]
Z_SIZE = 0.30  # depth of the medium (z-axis) [m]

N_X = math.floor(X_SIZE / dL)  # number of blocks in x-axis
N_Y = math.floor(Y_SIZE / dL)  # number of blocks in y-axis
N_Z = math.floor(Z_SIZE / dL)  # number of blocks in z-axis

G = 9.81  # acceleration due to gravity
THETA = 0.35  # porosity
KAPPA = 2.293577981651376e-10  # intrinsic permeability
MU = 9e-4  # dynamic viscosity
RHO = 1000  # density of water

MU_INVERSE = 1 / MU

RHO_G = RHO * G  # density of water times acceleration due to gravity

Q0 = 8e-5  # flux at the top of boundary [m/s]
FLUX_FULL = True  # set True for flux at whole top boundary, otherwise set false
FLUX_MIDDLE = False  # set True for flux at middle of top boundary (at 1 cm), otherwise set False

# The flux from the bottom boundary is set to zero if the saturation of the respective block does not exceed a residual
# saturation. Otherwise, equation (7) is used from the paper: https://doi.org/10.1038/s41598-021-82317-x Set residual
# saturation above 1.00 in the case of zero bottom boundary flux
# SATURATION_RESIDUAL = 0.05  # residual saturation
SATURATION_RESIDUAL = 1.05  # residual saturation

# hysteresis: the gradient of transition between the main branches of the retention curve hysteresis
Kps = 1e5

# Set True if you want to use van Genuchten retention curve. = recommended
# Set False if you want to use logistic retention curve. Used in: https://doi.org/10.1038/s41598-019-44831-x
GENUCHTEN = True

# Definition of the initial pressure. You can either start on the main wetting branch or main draining branch
WHICH_BRANCH = "wet"

# van Genuchten parameters for 20/30 sand
M_Q = 1 - 1 / VanGenuchtenWet.N
M_Q_inverse = 1 / M_Q

# Definition of the relative permeability
LAMBDA = 0.8

TIME_INTERVAL = 1.0  # define interval in [s]
LIM_VALUE = 0.999  # instead of unity, the value very close to unity is used

OUTPUT_DIR = "res"

# Definition of the time step
dtBase = 1e-3 * 0.25  # time step [s] for dL=0.01[m]
dt = (dx_PAR ** 2) * dtBase  # time step [s], typical choice of time step parameter for parabolic equation
RATIO = 1. / dx_PAR ** 2  # dtBase/dt=1./dx_par^2
SM = dt / (THETA * dL)  # [s/m] parameter
PRINT_MODULO = (RATIO * (1 / dtBase) * TIME_INTERVAL)

iteration = round(REALTIME / dt)  # number of iteration, t=dt*iter is REALTIME

PLOT_TIME = True  # Set True for time plot of porous media flow
SAVE_DATA = True  # Set True if you want to save Saturation and Pressure data

# Set true if you want to plot the basic retention curve and its linear modification which corresponds to the size of
# the block used for simulation
PLOT_RETENTION_CURVE = True

# DEFINITION OF THE REFERENCE BLOCK SIZE
# The crucial idea of the semi-continuum model is the scaling of the retention curve.
# For more details, see: https://doi.org/10.1038/s41598-022-11437-9

# Parameters A_WB, A_DB define the linear multiplication of the retention curve for the wetting and draining branches.
# Define the reference block size of the retention curve in centimeters
if GENUCHTEN:
    basic_block_size = 10.0 / 12.0  # the reference size of the block for 20/30 sand
else:
    basic_block_size = 1.0  # the reference size of the block for the logistic retention curve

A_RC = (1 / basic_block_size) * dx_PAR

# Output for the terminal.
print("Followed parameters are used for the simulation.")
print(f"Initial saturation [-]:                               {S0}")
print(f"Simulation time [s]:                                  {REALTIME}")
print(f"Size of the block used for simulation [cm]:           {dL * 100}")
print(f"Basic size of the block for the retention curve [cm]: {basic_block_size}")
print(f"Time step [s]:                                        {dt}")
print(f"Width and depth of the medium respectively [m]:       {X_SIZE},{Z_SIZE}")
print(f"Boundary flux [m/s]:                                  {Q0}")
print(("Van Genuchten" if GENUCHTEN else "Logistic") + "retention curve is used for the simulation.")

# DTYPE = np.longdouble  # = "float128"
DTYPE = np.double  # = "float64"
# DTYPE = np.single  # = "float32"

# Memory allocation
S = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Saturation matrix
S_new = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Saturation matrix for next iteration
S0_ini = np.ones((N_Y, N_Z, N_X), dtype=DTYPE)  # Initial saturation matrix

# Bottom boundary condition defined by residual saturation S_rs
bound_flux = np.zeros((N_Y, 1, N_X), dtype=DTYPE)
bound_lim = np.zeros((N_Y, 1, N_X), dtype=DTYPE)

Q_X = np.zeros((N_Y, N_Z, N_X + 1), dtype=DTYPE)  # Flux matrix for fluxes in x-axis
Q_Y = np.zeros((N_Y + 1, N_Z, N_X), dtype=DTYPE)  # Flux matrix for fluxes in y-axis
Q_Z = np.zeros((N_Y, N_Z + 1, N_X), dtype=DTYPE)  # Flux matrix for fluxes in z-axis
Q = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Flux matrix for/in "each block"

P = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Pressure matrix
P_wet = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Pressure for wetting curve
P_drain = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Pressure for draining curve
wet = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Logical variable for wetting mode
drain = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # Logical variable for draining mode

perm = np.zeros((N_Y, N_Z, N_X), dtype=DTYPE)  # relative permeability

# Distribution of intrinsic permeability - False if you don't want to have randomization of the intrinsic permeability
RANDOMIZATION_INTRINSIC_PERMEABILITY = True

# Two different methods of randomization of intrinsic permeability: filter and interpolation methods. 
# Interpolation method is recommended - Kmec, J.: Analysis of the mathematical models for unsaturated porous media flow
METHOD_FILTER = False  # filter method
METHOD_INTERPOLATION = True  # interpolation method
KERNEL_SIZE = 6

LOAD_FROM_FILE = False  # If you already have distribution of intrinsic permeability defined in the file

if not os.path.exists(OUTPUT_DIR):
    os.mkdir(OUTPUT_DIR)

with open(f"{OUTPUT_DIR}/parameters.json", "w") as f:
    json.dump({
        "x": X_SIZE, "y": Y_SIZE, "z": Z_SIZE, "realtime": REALTIME, "dx": dL, "initial_saturation": S0,
        "flux": {"middle": FLUX_MIDDLE, "full": FLUX_FULL, "dt": dtBase}
    }, f)

if LOAD_FROM_FILE:
    random_perm = np.load("random_perm.npy")

elif RANDOMIZATION_INTRINSIC_PERMEABILITY:
    if METHOD_FILTER:  # TODO 3D
        random_perm = np.random.normal(0, 1, S.shape) * 0.8
        random_perm = cv2.filter2D(random_perm, -1, np.ones((KERNEL_SIZE, KERNEL_SIZE), np.float64) / KERNEL_SIZE)

        if len(random_perm) == 1:
            sns.heatmap(random_perm)
            plt.title("Randomized intrinsic permeability")
            plt.savefig(f"{OUTPUT_DIR}/random_perm_filter.png")
            plt.clf()
        else:
            tifffile.imwrite(f"{OUTPUT_DIR}/random_perm_filter.tif", random_perm)

        np.save("random_perm_filter.npy", random_perm)

    elif METHOD_INTERPOLATION:
        # Define intrinsic permeability for the blocks of the size 2.5cm
        block_par = 0.025

        interpolation_blocks = block_par / dL
        random_perm = np.random.normal(0, 1, [
            math.ceil(Y_SIZE / block_par),
            math.ceil(Z_SIZE / block_par),
            math.ceil(X_SIZE / block_par)]) * 0.3

        if len(random_perm) == 1:
            sns.heatmap(random_perm)
            plt.title("Randomized intrinsic permeability - before")
            plt.savefig(f"{OUTPUT_DIR}/random_perm_interpolation_before.png")
            plt.clf()
        else:
            tifffile.imwrite(f"{OUTPUT_DIR}/random_perm_interpolation_before.tif", random_perm)

        random_perm = zoom(random_perm.get() if USE_GPU else random_perm, (interpolation_blocks, interpolation_blocks, interpolation_blocks))

        if len(random_perm) == 1:
            sns.heatmap(random_perm)
            plt.title("Randomized intrinsic permeability - after")
            plt.show()
            plt.savefig(f"{OUTPUT_DIR}/random_perm_interpolation_after.png")
            plt.clf()
        else:
            tifffile.imwrite(f"{OUTPUT_DIR}/random_perm_interpolation_after.tif", random_perm)

        np.save(f"{OUTPUT_DIR}/random_perm_interpolation.npy", random_perm)

if RANDOMIZATION_INTRINSIC_PERMEABILITY:
    multiply = np.zeros_like(random_perm)
    multiply[random_perm > 0] = (1 + random_perm[random_perm > 0])
    multiply[random_perm < 0] = (1. / (1 - random_perm[random_perm < 0]))
else:
    multiply = np.ones_like(S)

k_rnd = KAPPA * multiply
k_rnd_sqrt = np.sqrt(k_rnd)
k_rnd_q1 = MU_INVERSE * k_rnd_sqrt[:N_Y, :N_Z, :N_X - 1] * k_rnd_sqrt[:N_Y, :N_Z, 1:N_X]
k_rnd_q2 = MU_INVERSE * k_rnd_sqrt[:N_Y, :N_Z - 1, :N_X] * k_rnd_sqrt[:N_Y, 1:N_Z, :N_X]
k_rnd_q3 = MU_INVERSE * k_rnd_sqrt[:N_Y - 1, :N_Z, :N_X] * k_rnd_sqrt[1:N_Y, :N_Z, :N_X]

print("################# INTRINSIC PERMEABILITY #################")
if RANDOMIZATION_INTRINSIC_PERMEABILITY:
    print(f"The minimum of random_perm is {np.amin(random_perm)} and maximum is {np.amax(random_perm)}")
    print(f"The minimum of the intrinsic permeability is {np.amin(k_rnd)} and maximum is {np.amax(k_rnd)}")
    print(f"The average is {np.mean(k_rnd)} and predefined intrinsic permeability respectively is {KAPPA}")
else:
    print("The distribution of the intrinsic permeability is not used.")

print("#" * 20)

# Initialization of porous media flow
# Initial saturation
S0_ini = S0_ini * S0
S = S0_ini

# Definition of top boundary condition: three possibilities can be chosen.
if FLUX_FULL and not FLUX_MIDDLE:  # Flux q0 at whole top boundary.
    Q_Z[:, 0, :] = Q0

elif not FLUX_FULL and FLUX_MIDDLE:  # Flux q0 at the middle at 1 cm.
    middle_x = (X_SIZE - 0.01) / 2
    middle_y = (Y_SIZE - 0.01) / 2
    pom_x = round(middle_x / dL)
    pom_y = round(middle_y / dL)
    vec = round(0.01 / dL)

    Q_Z[pom_y:pom_y, 0, pom_x: pom_x + vec] = Q0

else:  # Flux q0 only in the middle block.
    Q_Z[round(N_Y / 2), 0, round(N_X / 2)] = Q0

retention_curve_wet = (VanGenuchtenWet(A_RC, RHO_G) if GENUCHTEN else RetentionCurveWet(A_RC)).calculate
retention_curve_drain = (VanGenuchtenDrain(A_RC, RHO_G) if GENUCHTEN else RetentionCurveDrain(A_RC)).calculate

# Initial capillary pressure. For a parameter
#   - 'wet' we start on the main wetting branch
#   - 'drain' we start on the main draining branch.
P = retention_curve_wet(S) if WHICH_BRANCH == "wet" else retention_curve_drain(S)

bound_residual = np.ones((N_Y, 1, N_X), dtype=DTYPE) * SATURATION_RESIDUAL

time_start = time.time()
print()

avg = []
# Main part - saturation, pressure and flux update
for k in tqdm(range(1, iteration+1)):
    # --------------- SATURATION UPDATE ---------------
    Q[:N_Y, :N_Z, :N_X] = Q_X[:N_Y, :N_Z, :N_X] - Q_X[:N_Y, :N_Z, 1:N_X + 1] \
                          + Q_Y[:N_Y, :N_Z, :N_X] - Q_Y[1:N_Y + 1, :N_Z, :N_X] \
                          + Q_Z[:N_Y, :N_Z, :N_X] - Q_Z[:N_Y, 1:N_Z + 1, :N_X]
                    
    S_new = S + SM * Q

    # If the flux is too large, then the saturation would increase over unity.
    # In 1D, we simply returned excess water to the block it came from. This approach should be generalized in 2D in
    # such a way that excess water is returned from where it came proportionally to the fluxes. Here we use only the
    # implementation provided for the 1D case. Thus water is returned only above. However, for all the 2D simulations
    # published or are in reviewing process, saturation had never reached unity so this implementation was not used.

    # while np.abs(np.amax(S_new)) > LIM_VALUE:
    #     print("Error - that should not happen")
    #     S_over = np.zeros((n_Y, n_Z, n_X), dtype=DTYPE)
    #     S_over[S_new > LIM_VALUE] = S_new[S_new > LIM_VALUE] - LIM_VALUE
    #     S_new[S_new > LIM_VALUE] = LIM_VALUE
    
    #     id1, id2, id3 = np.nonzero(S_over[:, 1:, :] > 0)
    
    #     for i in range(len(id1)):
    #         S_new[id1[i], id2[i], id3[i]] = S_new[id1[i], id2[i], id3[i]] + S_over[id1[i], id2[i] + 1, id3[i]]

    # Bottom boundary condition residual saturation is used
    bound_lim = np.minimum(S_new[:, N_Z - 1:N_Z, :], bound_residual)
    S_new[:, N_Z - 1:N_Z, :] = np.maximum(S_new[:, N_Z - 1:N_Z, :] + SM * bound_flux, bound_lim)

    # --------------- PRESSURE UPDATE ---------------
    # Hysteresis
    P = P + Kps * (S_new - S)

    P_wet = retention_curve_wet(S)
    P_drain = retention_curve_drain(S)

    wet = (S_new - S) > 0  # logical matrix for wetting branch
    drain = (S - S_new) > 0  # logical matrix for draining branch

    P[wet] = np.minimum(P[wet], P_wet[wet])
    P[drain] = np.maximum(P[drain], P_drain[drain])

    # --------------- FLUX UPDATE ---------------
    # Calculate relative permeability Side fluxes at boundary are set to zero.
    perm = S_new ** LAMBDA * (1 - (1 - S_new ** M_Q_inverse) ** M_Q) ** 2
    perm_sqrt = np.sqrt(perm)

    Q_X[:, :, 1:N_X] = k_rnd_q1 * \
                       perm_sqrt[:N_Y, :N_Z, :N_X - 1] * \
                       perm_sqrt[:N_Y, :N_Z, 1:N_X] * \
                       (- ((P[:N_Y, :N_Z, 1:N_X] - P[:N_Y, :N_Z, :N_X - 1]) / dL))

    Q_Y[1:N_Y, :, :] = k_rnd_q3 * \
                       perm_sqrt[:N_Y - 1, :N_Z, :N_X] * \
                       perm_sqrt[1:N_Y, :N_Z, :N_X] * \
                       (- ((P[1:N_Y, :N_Z, :N_X] - P[:N_Y - 1, :N_Z, :N_X]) / dL))

    Q_Z[:, 1:N_Z, :] = k_rnd_q2 * \
                       perm_sqrt[:N_Y, :N_Z - 1, :N_X] * \
                       perm_sqrt[:N_Y, 1:N_Z, :N_X] * \
                       (RHO_G - ((P[:N_Y, 1:N_Z, :N_X] - P[:N_Y, :N_Z - 1, :N_X]) / dL))

    S = S_new

    # Calculation of flux at bottom boundary.
    bound_flux[:, 0, :] = MU_INVERSE * k_rnd[:N_Y, N_Z - 1, :N_X] * perm[:N_Y, N_Z - 1, :N_X] * (RHO_G - ((0 - P[:N_Y, N_Z - 1, :N_X]) / dL))

    # --------------- Saving data and check mass balance law ---------------
    if k % PRINT_MODULO == 0:
        t = round(k * dt) - 1  # calculation a real simulation time

        # Data saving
        if SAVE_DATA:
            np.save(f"{OUTPUT_DIR}/saturation_{t}.npy", S)
            np.save(f"{OUTPUT_DIR}/pressure_{t}.npy", P)
            np.save(f"{OUTPUT_DIR}/Q_X_{t}.npy", Q_X)
            np.save(f"{OUTPUT_DIR}/Q_Y_{t}.npy", Q_Y)
            np.save(f"{OUTPUT_DIR}/Q_Z_{t}.npy", Q_Z)
            np.save(f"{OUTPUT_DIR}/Q_{t}.npy", Q)

        # Check the mass balance law
        flowed_in_real = np.sum(S) - np.sum(S0_ini)
        flowed_in_should = k * SM * np.sum(Q_Z[:,0,:])
        error_abs = abs(flowed_in_real - flowed_in_should)
        error_rel = error_abs/flowed_in_should
        print(f"Error in saturation:\n\t- absolute:\t {error_abs}\n\t- relative:\t{error_rel}")

        # Information of calculated simulation time printed on the terminal.
        print(f"Simulation time is {t+1} s, simulation is running for {time.time() - time_start} s")

    # Only for a code testing purpose.
    if np.abs(np.amax(S)) > LIM_VALUE:
        raise Exception(f"Saturation is over {LIM_VALUE} defined in the code.")

print(f"The simulation lasted: {time.time() - time_start} s")

# Time plot in two/three dimensions
if PLOT_TIME:
    if N_Y == 1:  # 2D or 1D
        saturation = [np.load(f"{OUTPUT_DIR}/saturation_{t}.npy") for t in range(REALTIME)]
        pressure = [np.load(f"{OUTPUT_DIR}/pressure_{t}.npy") for t in range(REALTIME)]

        if N_X == 1:  # 1D
            new_saturation, new_pressure = [], []
            for t in range(REALTIME):
                new_saturation.append(cv2.resize(saturation[t], None, fy=1, fx=10, interpolation=cv2.INTER_NEAREST))
                new_pressure.append(cv2.resize(pressure[t], None, fy=1, fx=10, interpolation=cv2.INTER_NEAREST))
            saturation, pressure = np.array(new_saturation, dtype=DTYPE), np.array(new_pressure, dtype=DTYPE)

        saturation = np.squeeze(saturation * 100, axis=1)
        pressure = np.squeeze(pressure, axis=1)

        px.imshow(
            saturation.get() if USE_GPU else saturation, zmin=0, zmax=100, animation_frame=0, title="Saturation visualization over time",
            labels={"x": "Length", "y": "Depth", "color": "Saturation [%]", "animation_frame": "Time [s]"},
            color_continuous_scale='gray'
        ).write_html(f"{OUTPUT_DIR}/saturation.html")

        px.imshow(
            pressure.get() if USE_GPU else pressure, animation_frame=0, title="Pressure visualization over time",
            labels={"x": "Length", "y": "Depth", "color": "Pressure", "animation_frame": "Time [s]"},
            color_continuous_scale='gray'
        ).write_html(f"{OUTPUT_DIR}/pressure.html")
    else:
        pass  # TODO 3D

# Plot the basic retention curve and its linear modification defined by the scaling of the retention curve
if PLOT_RETENTION_CURVE:
    SS = np.arange(0.001, 0.999, 0.001)
    SS = SS.get() if USE_GPU else SS

    P_wet = VanGenuchtenWet(1, RHO_G).calculate(SS)
    P_drain = VanGenuchtenDrain(1, RHO_G).calculate(SS)
    plt.plot(SS, P_wet, color="r", label="Basic WB")
    plt.plot(SS, P_drain, color="k", label="Basic DB")

    P1 = VanGenuchtenWet(A_RC, RHO_G).calculate(SS)
    P2 = VanGenuchtenDrain(A_RC, RHO_G).calculate(SS)
    plt.plot(SS, P1, color="r", linestyle="dashed", label="Updated WB: scaled retention curve")
    plt.plot(SS, P2, color="k", linestyle="dashed", label="Updated DB: scaled retention curve")

    plt.title("Retention curves")
    plt.legend()
    plt.show()
