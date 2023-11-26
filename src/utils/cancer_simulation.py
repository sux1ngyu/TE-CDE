"""
CODE ADAPTED FROM: https://github.com/sjblim/rmsn_nips_2018 &
https://github.com/ioanabica/Counterfactual-Recurrent-Network

Medically realistic data simulation for small-cell lung cancer based on Geng et al 2017.
URL: https://www.nature.com/articles/s41598-017-13646-z

Notes:
- Simulation time taken to be in days

"""

import logging
import os
import pickle

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import (
    truncnorm,  # we need to sample from truncated normal distributions
)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Simulation Constants

# Spherical calculations - tumours assumed to be spherical per Winer-Muram et al 2002.
# URL: https://pubs.rsna.org/doi/10.1148/radiol.2233011026?url_ver=Z39.88-2003&rfr_id=ori%3Arid%3Acrossref.org&rfr_dat=cr_pub%3Dpubmed
def calc_volume(diameter):
    return 4.0 / 3.0 * np.pi * (diameter / 2.0) ** 3.0


def calc_diameter(volume):
    return ((volume / (4.0 / 3.0 * np.pi)) ** (1.0 / 3.0)) * 2.0


# Tumour constants per
tumour_cell_density = 5.8 * 10.0**8.0  # cells per cm^3
tumour_death_threshold = calc_volume(13)  # assume spherical

# Patient cancer stage. (mu, sigma, lower bound, upper bound) - for lognormal dist
tumour_size_distributions = {
    "I": (1.72, 4.70, 0.3, 5.0),
    "II": (1.96, 1.63, 0.3, 13.0),
    "IIIA": (1.91, 9.40, 0.3, 13.0),
    "IIIB": (2.76, 6.87, 0.3, 13.0),
    "IV": (3.86, 8.82, 0.3, 13.0),
}  # 13.0 is the death condition
# 这里就是定义好每个类型的肿瘤最开始的正态分布，每个病人最开始肿瘤大小是由这个来生成的
# 且定义好最小和最大值，超过13就会死亡了，低于0.3可能不能算肿瘤。当然会调整lower和upper bound让他符合正态分布的情况

# Observations of stage proportions taken from Detterbeck and Gibson 2008
# - URL: http://www.jto.org/article/S1556-0864(15)33353-0/fulltext#cesec50\
cancer_stage_observations = {
    "I": 1432,
    "II": 128,
    "IIIA": 1306,
    "IIIB": 7248,
    "IV": 12840,
}
# 定义每个类型肿瘤的人数，从而使得我们的synthetic dataset至少在人数比例上能够符合实际的情况


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Simulation Functions


def get_confounding_params(num_patients, chemo_coeff, radio_coeff):
    """

    Get original simulation parameters, and add extra ones to control confounding
    这个部分给的treatment assignment 参数，intercept就是减去的那个值，设置为Dmax/2
    而系数是chemo_coeff和radio_coeff设定好了的
    :param num_patients:
    :param chemo_coeff: Bias on action policy for chemotherapy assignments
    :param radio_activation_group: Bias on action policy for chemotherapy assignments
    :return:
    """

    basic_params = get_standard_params(num_patients)
    patient_types = basic_params["patient_types"]
    tumour_stage_centres = [s for s in cancer_stage_observations if "IIIA" not in s]
    # 相当于除去了IIIA型，为什么要把这个型扔掉呢
    tumour_stage_centres.sort()

    d_max = calc_diameter(tumour_death_threshold)  # 其实就是13
    basic_params["chemo_sigmoid_intercepts"] = np.array(
        [d_max / 2.0 for i in patient_types],
    )
    basic_params["radio_sigmoid_intercepts"] = np.array(
        [d_max / 2.0 for i in patient_types],
    )

    basic_params["chemo_sigmoid_betas"] = np.array(
        [chemo_coeff / d_max for i in patient_types],
    )
    basic_params["radio_sigmoid_betas"] = np.array(
        [radio_coeff / d_max for i in patient_types],
    )

    return basic_params


def get_standard_params(num_patients):  # additional params
    """
    Simulation parameters from the Nature article + adjustments for static variables
    考虑了tumor growth model里面最基本的参数，以及考虑了不同人群的treatment response
    :param num_patients:
    :return: simulation_parameters
    """

    # Adjustments for static variables
    # 产生三组不同的treatment response
    possible_patient_types = [1, 2, 3]
    patient_types = np.random.choice(possible_patient_types, num_patients)
    chemo_mean_adjustments = np.array([0.0 if i < 3 else 0.1 for i in patient_types])
    radio_mean_adjustments = np.array([0.0 if i > 1 else 0.1 for i in patient_types])

    total = 0
    for k in cancer_stage_observations:
        total += cancer_stage_observations[k]
    cancer_stage_proportions = {
        k: float(cancer_stage_observations[k]) / float(total)
        for k in cancer_stage_observations
    }  # 根据实际的病人观测，得到肿瘤每个阶段的病人大概占总体的比例

    # remove possible entries
    possible_stages = list(tumour_size_distributions.keys())
    possible_stages.sort()

    initial_stages = np.random.choice(
        possible_stages,
        num_patients,
        p=[cancer_stage_proportions[k] for k in possible_stages],
    )
    # 相当于产生长度10000的list，list的内容是肿瘤的型，代表每个病人最开始的肿瘤类型
    # 分配的概率是根据上面实际观测的比例，使得最后每个类型的病人人数比例和真实情况尽可能相符合

    # Get info on patient stages and initial volumes
    # 得到每个病人最开始的肿瘤类型，并根据肿瘤类型得到这个类型的体积大小分布，并得到最开始的体积大小
    output_initial_diam = []
    patient_sim_stages = []
    for stg in possible_stages:
        count = np.sum((initial_stages == stg) * 1)

        mu, sigma, lower_bound, upper_bound = tumour_size_distributions[stg]

        # Convert lognorm bounds in to standard normal bounds
        lower_bound = (np.log(lower_bound) - mu) / sigma
        upper_bound = (np.log(upper_bound) - mu) / sigma

        logging.info(
            (
                "Simulating initial volumes for stage {} "
                + " with norm params: mu={}, sigma={}, lb={}, ub={}"
            ).format(stg, mu, sigma, lower_bound, upper_bound),
        )

        norm_rvs = truncnorm.rvs(
            lower_bound,
            upper_bound,
            size=count,
        )  # truncated normal for realistic clinical outcome
        # 这里就是生成lower bound 到 upper bound，均值=0方差=1的这么多个数据点

        initial_volume_by_stage = np.exp((norm_rvs * sigma) + mu)  # 得到最开始的肿瘤体积大小
        output_initial_diam += list(initial_volume_by_stage)
        patient_sim_stages += [stg for i in range(count)]

    # Fixed params
    K = calc_volume(30)  # carrying capacity given in cm, so convert to volume
    alpha_beta_ratio = 10
    alpha_rho_corr = 0.87

    # Distributional parameters for dynamics
    parameter_lower_bound = 0.0
    parameter_upper_bound = np.inf
    rho_params = (7 * 10**-5, 7.23 * 10**-3)
    alpha_params = (0.0398, 0.168)
    beta_c_params = (0.028, 0.0007)
    # 产生的是normal dist的分布参数

    # Get correlated simulation paramters (alpha, beta, rho) which respects bounds
    # 就是得到alpha和rho之间的协方差，rho是tumor自然增长的参数，alpha则是radio杀死细胞的参数
    alpha_rho_cov = np.array(
        [
            [alpha_params[1] ** 2, alpha_rho_corr * alpha_params[1] * rho_params[1]],
            [alpha_rho_corr * alpha_params[1] * rho_params[1], rho_params[1] ** 2],
        ],
    )

    alpha_rho_mean = np.array([alpha_params[0], rho_params[0]])

    simulated_params = []

    while (
        len(simulated_params) < num_patients
    ):  # Keep on simulating till we get the right number of params

        param_holder = np.random.multivariate_normal(
            alpha_rho_mean,
            alpha_rho_cov,
            size=num_patients,
        )
        # 10000 * 2的size，根据alpha和rho的covariances来生成，相当于每个病人都有alpha和rho的值了

        for i in range(param_holder.shape[0]):

            # Ensure that all params fulfill conditions
            if (
                param_holder[i, 0] > parameter_lower_bound
                and param_holder[i, 1] > parameter_lower_bound
            ):
                simulated_params.append(param_holder[i, :])

        logging.info(
            "Got correlated params for {} patients".format(len(simulated_params)),
        )

    simulated_params = np.array(simulated_params)[
        :num_patients, :
    ]  # shorten this back to normal
    alpha_adjustments = alpha_params[0] * radio_mean_adjustments
    alpha = simulated_params[:, 0] + alpha_adjustments
    # 这里就是产生three group of patients，把group 1的alpha r乘以1.1
    rho = simulated_params[:, 1]
    beta = alpha / alpha_beta_ratio

    # Get the remaining indep params
    logging.info("Simulating beta c parameters")
    beta_c_adjustments = beta_c_params[0] * chemo_mean_adjustments
    beta_c = (
        beta_c_params[0]
        + beta_c_params[1]
        * truncnorm.rvs(
            (parameter_lower_bound - beta_c_params[0]) / beta_c_params[1],
            (parameter_upper_bound - beta_c_params[0]) / beta_c_params[1],
            size=num_patients,
        )
        + beta_c_adjustments
    )
    # 这里将group 3的beta c * 1.1
    # beta c是 chemo治疗的参数

    output_holder = {
        "patient_types": patient_types,
        "initial_stages": np.array(patient_sim_stages),
        "initial_volumes": calc_volume(
            np.array(output_initial_diam),
        ),  # assumed spherical with diam
        "alpha": alpha,
        "rho": rho,
        "beta": beta,
        "beta_c": beta_c,
        "K": np.array([K for i in range(num_patients)]),
    }
    # np.random.exponential(expected_treatment_delay, num_patients),

    # Randomise output params
    # 按照病人的维度打乱
    logging.info("Randomising outputs")
    idx = [i for i in range(num_patients)]
    np.random.shuffle(idx)

    output_params = {}
    for k in output_holder:
        output_params[k] = output_holder[k][idx]

    return output_params


def simulate(simulation_params, num_time_steps, assigned_actions=None):
    """
    Core routine to generate simulation paths

    :param simulation_params:
    :param num_time_steps:
    :param assigned_actions:
    :return:
    """

    total_num_radio_treatments = 1
    total_num_chemo_treatments = 1

    radio_amt = np.array([2.0 for i in range(total_num_radio_treatments)])  # Gy
    radio_days = np.array([i + 1 for i in range(total_num_radio_treatments)])
    chemo_amt = [5.0 for i in range(total_num_chemo_treatments)]
    chemo_days = [(i + 1) * 7 for i in range(total_num_chemo_treatments)]

    # sort this
    chemo_idx = np.argsort(chemo_days)
    chemo_amt = np.array(chemo_amt)[chemo_idx]
    chemo_days = np.array(chemo_days)[chemo_idx]

    drug_half_life = 1  # one day half life for drugs

    # Unpack simulation parameters
    initial_stages = simulation_params["initial_stages"]
    initial_volumes = simulation_params["initial_volumes"]
    alphas = simulation_params["alpha"]
    rhos = simulation_params["rho"]
    betas = simulation_params["beta"]
    beta_cs = simulation_params["beta_c"]
    Ks = simulation_params["K"]
    patient_types = simulation_params["patient_types"]
    window_size = simulation_params[
        "window_size"
    ]  # controls the lookback of the treatment assignment policy

    # Coefficients for treatment assignment probabilities
    chemo_sigmoid_intercepts = simulation_params["chemo_sigmoid_intercepts"]
    radio_sigmoid_intercepts = simulation_params["radio_sigmoid_intercepts"]
    chemo_sigmoid_betas = simulation_params["chemo_sigmoid_betas"]
    radio_sigmoid_betas = simulation_params["radio_sigmoid_betas"]

    num_patients = initial_stages.shape[0]

    # Commence Simulation
    cancer_volume = np.zeros((num_patients, num_time_steps))
    chemo_dosage = np.zeros((num_patients, num_time_steps))
    radio_dosage = np.zeros((num_patients, num_time_steps))
    chemo_application_point = np.zeros((num_patients, num_time_steps))
    radio_application_point = np.zeros((num_patients, num_time_steps))
    sequence_lengths = np.zeros(num_patients)
    death_flags = np.zeros((num_patients, num_time_steps))
    recovery_flags = np.zeros((num_patients, num_time_steps))
    chemo_probabilities = np.zeros((num_patients, num_time_steps))
    radio_probabilities = np.zeros((num_patients, num_time_steps))

    noise_terms = 0.01 * np.random.randn(
        num_patients,
        num_time_steps,
    )  # 5% cell variability
    recovery_rvs = np.random.rand(num_patients, num_time_steps)

    chemo_application_rvs = np.random.rand(num_patients, num_time_steps)
    radio_application_rvs = np.random.rand(num_patients, num_time_steps)
    # 相当于先把做chemo和radio的概率算出来，到时候在跟threshold比，比threshold大，就assign treatment

    # Run actual simulation
    for i in range(num_patients):
        if i % 200 == 0:
            logging.info("Simulating patient {} of {}".format(i, num_patients))
        noise = noise_terms[i]

        # initial values
        cancer_volume[i, 0] = initial_volumes[i]
        alpha = alphas[i]
        beta = betas[i]
        beta_c = beta_cs[i]
        rho = rhos[i]
        K = Ks[i]

        # Setup cell volume
        b_death = False
        b_recover = False
        for t in range(0, num_time_steps - 1):

            current_chemo_dose = 0.0
            previous_chemo_dose = 0.0 if t == 0 else chemo_dosage[i, t - 1]

            # Action probabilities + death or recovery simulations
            cancer_volume_used = cancer_volume[i, max(t - window_size, 0) : t + 1]
            cancer_diameter_used = np.array(
                [calc_diameter(vol) for vol in cancer_volume_used],
            ).mean()  # mean diameter over 15 days
            cancer_metric_used = cancer_diameter_used

            # probabilities
            if assigned_actions is not None:
                chemo_prob = assigned_actions[i, t, 0]
                radio_prob = assigned_actions[i, t, 1]
            else:

                radio_prob = 1.0 / (
                    1.0
                    + np.exp(
                        -radio_sigmoid_betas[i]
                        * (cancer_metric_used - radio_sigmoid_intercepts[i]),
                    )
                )  # 这里其实就是treatment assignment的概率计算，选择了使用sigmoid函数激活
                chemo_prob = 1.0 / (
                    1.0
                    + np.exp(
                        -chemo_sigmoid_betas[i]
                        * (cancer_metric_used - chemo_sigmoid_intercepts[i]),
                    )
                )
            chemo_probabilities[i, t] = chemo_prob
            radio_probabilities[i, t] = radio_prob

            # Action application
            # 如果满足条件，就施加radio或者chemo治疗
            if radio_application_rvs[i, t] < radio_prob:
                radio_application_point[i, t] = 1
                radio_dosage[i, t] = radio_amt[0]

            if chemo_application_rvs[i, t] < chemo_prob:
                # Apply chemo treatment
                chemo_application_point[i, t] = 1
                current_chemo_dose = chemo_amt[0]

            # Update chemo dosage
            chemo_dosage[i, t] = (
                previous_chemo_dose * np.exp(-np.log(2) / drug_half_life)
                + current_chemo_dose
            )
            # 这里就是chemo浓度本身的变化，当然可能这个时间点有新加的chemo

            cancer_volume[i, t + 1] = cancer_volume[i, t] * (
                1
                + +rho * np.log(K / cancer_volume[i, t])
                - beta_c * chemo_dosage[i, t]
                - (alpha * radio_dosage[i, t] + beta * radio_dosage[i, t] ** 2)
                + noise[t]
            )  # add noise to fit residuals
            # 所以每个时间点的肿瘤大小就知道了

            if cancer_volume[i, t + 1] > tumour_death_threshold:
                cancer_volume[i, t + 1] = tumour_death_threshold
                break  # patient death

            # recovery threshold as defined by the previous stuff
            if recovery_rvs[i, t + 1] < np.exp(
                -cancer_volume[i, t + 1] * tumour_cell_density,
            ):
                cancer_volume[i, t + 1] = 0
                break

        # Package outputs
        sequence_lengths[i] = int(t + 1)

    outputs = {
        "cancer_volume": cancer_volume,
        "chemo_dosage": chemo_dosage,
        "radio_dosage": radio_dosage,
        "chemo_application": chemo_application_point,
        "radio_application": radio_application_point,
        "chemo_probabilities": chemo_probabilities,
        "radio_probabilities": radio_probabilities,
        "sequence_lengths": sequence_lengths,
        "patient_types": patient_types,
    }
    # 这个地方的cancer volume就包括所有病人所有时间的肿瘤大小了了，当然有可能death或者康复了后面的时间就没有记录
    # dosage应该是浓度大小，application是施加treatment与否

    return outputs


def simulate_counterfactual_test_data(
    simulation_params,
    num_time_steps,
    assigned_actions=None,
):
    """
    Core routine to generate simulation test paths to asses all of the counterfactuals.
    就是对于原来的factual产生synthetic dataset的过程，迭代所有的treatment，产生对应的tumor大小
    :param simulation_params:
    :param num_time_steps:
    :param assigned_actions:
    :return:
    """
    np.random.seed(100)

    total_num_radio_treatments = 1
    total_num_chemo_treatments = 1

    num_treatments = (
        4  # No treatment/Chemotherapy/Radiotherapy/Chemotherapy + Radiotherapy
    )

    radio_amt = np.array([2.0 for i in range(total_num_radio_treatments)])  # Gy
    radio_days = np.array([i + 1 for i in range(total_num_radio_treatments)])
    chemo_amt = [5.0 for i in range(total_num_chemo_treatments)]
    chemo_days = [(i + 1) * 7 for i in range(total_num_chemo_treatments)]

    # sort this
    chemo_idx = np.argsort(chemo_days)
    chemo_amt = np.array(chemo_amt)[chemo_idx]
    chemo_days = np.array(chemo_days)[chemo_idx]

    drug_half_life = 1  # one day half life for drugs

    # Unpack simulation parameters
    initial_stages = simulation_params["initial_stages"]
    initial_volumes = simulation_params["initial_volumes"]
    alphas = simulation_params["alpha"]
    rhos = simulation_params["rho"]
    betas = simulation_params["beta"]
    beta_cs = simulation_params["beta_c"]
    Ks = simulation_params["K"]
    patient_types = simulation_params["patient_types"]
    window_size = simulation_params[
        "window_size"
    ]  # controls the lookback of the treatment assignment policy

    # Coefficients for treatment assignment probabilities
    chemo_sigmoid_intercepts = simulation_params["chemo_sigmoid_intercepts"]
    radio_sigmoid_intercepts = simulation_params["radio_sigmoid_intercepts"]
    chemo_sigmoid_betas = simulation_params["chemo_sigmoid_betas"]
    radio_sigmoid_betas = simulation_params["radio_sigmoid_betas"]

    num_patients = initial_stages.shape[0]

    num_test_points = num_patients * num_time_steps * num_treatments

    # Commence Simulation
    cancer_volume = np.zeros((num_test_points, num_time_steps))
    chemo_application_point = np.zeros((num_test_points, num_time_steps))
    radio_application_point = np.zeros((num_test_points, num_time_steps))
    sequence_lengths = np.zeros(num_test_points)
    patient_types_all_trajectories = np.zeros(num_test_points)

    test_idx = 0

    # Run actual simulation
    for i in range(num_patients):

        if i % 200 == 0:
            logging.info("Simulating patient {} of {}".format(i, num_patients))

        noise = 0.01 * np.random.randn(num_time_steps)  # 5% cell variability
        recovery_rvs = np.random.rand(num_time_steps)

        # initial values
        factual_cancer_volume = np.zeros(num_time_steps)
        factual_chemo_dosage = np.zeros(num_time_steps)
        factual_radio_dosage = np.zeros(num_time_steps)
        factual_chemo_application_point = np.zeros(num_time_steps)
        factual_radio_application_point = np.zeros(num_time_steps)
        factual_chemo_probabilities = np.zeros(num_time_steps)
        factual_radio_probabilities = np.zeros(num_time_steps)

        chemo_application_rvs = np.random.rand(num_time_steps)
        radio_application_rvs = np.random.rand(num_time_steps)

        factual_cancer_volume[0] = initial_volumes[i]

        alpha = alphas[i]
        beta = betas[i]
        beta_c = beta_cs[i]
        rho = rhos[i]
        K = Ks[i]

        for t in range(0, num_time_steps - 1):

            # Factual treatments and outcomes
            current_chemo_dose = 0.0
            previous_chemo_dose = 0.0 if t == 0 else factual_chemo_dosage[t - 1]

            # Action probabilities + death or recovery simulations
            cancer_volume_used = cancer_volume[i, max(t - window_size, 0) : t + 1]
            cancer_diameter_used = np.array(
                [calc_diameter(vol) for vol in cancer_volume_used],
            ).mean()  # mean diameter over 15 days
            cancer_metric_used = cancer_diameter_used

            # probabilities
            radio_prob = 1.0 / (
                1.0
                + np.exp(
                    -radio_sigmoid_betas[i]
                    * (cancer_metric_used - radio_sigmoid_intercepts[i]),
                )
            )
            chemo_prob = 1.0 / (
                1.0
                + np.exp(
                    -chemo_sigmoid_betas[i]
                    * (cancer_metric_used - chemo_sigmoid_intercepts[i]),
                )
            )

            factual_chemo_probabilities[t] = chemo_prob
            factual_radio_probabilities[t] = radio_prob

            # Action application
            if radio_application_rvs[t] < radio_prob:
                factual_radio_application_point[t] = 1
                factual_radio_dosage[t] = radio_amt[0]

            if chemo_application_rvs[t] < chemo_prob:
                factual_chemo_application_point[t] = 1
                current_chemo_dose = chemo_amt[0]

            # Update chemo dosage
            factual_chemo_dosage[t] = (
                previous_chemo_dose * np.exp(-np.log(2) / drug_half_life)
                + current_chemo_dose
            )

            # Factual treatments and outcomes
            factual_cancer_volume[t + 1] = factual_cancer_volume[t] * (
                1
                + rho * np.log(K / factual_cancer_volume[t])
                - beta_c * factual_chemo_dosage[t]
                - (
                    alpha * factual_radio_dosage[t]
                    + beta * factual_radio_dosage[t] ** 2
                )
                + noise[t + 1]
            )  # add noise to fit residuals

            factual_cancer_volume[t + 1] = np.clip(
                factual_cancer_volume[t + 1],
                0,
                tumour_death_threshold,
            )

            # Popoulate arrays
            cancer_volume[test_idx] = factual_cancer_volume
            chemo_application_point[test_idx] = factual_chemo_application_point
            radio_application_point[test_idx] = factual_radio_application_point
            patient_types_all_trajectories[test_idx] = patient_types[i]
            sequence_lengths[test_idx] = int(t) + 1
            test_idx = test_idx + 1

            # Counterfactual treatments and outcomes
            treatment_options = [
                (0, 0),
                (0, 1),
                (1, 0),
                (1, 1),
            ]  # First = chemo; second = radio

            for treatment_option in treatment_options:
                if (
                    factual_chemo_application_point[t] == treatment_option[0]
                    and factual_radio_application_point[t] == treatment_option[1]
                ):
                    # This represents the factual treatment which was already considered
                    continue
                current_chemo_dose = 0.0
                counterfactual_radio_dosage = 0.0
                counterfactual_chemo_application_point = 0
                counterfactual_radio_application_point = 0

                if treatment_option[0] == 1:
                    counterfactual_chemo_application_point = 1
                    current_chemo_dose = chemo_amt[0]

                if treatment_option[1] == 1:
                    counterfactual_radio_application_point = 1
                    counterfactual_radio_dosage = radio_amt[0]

                counterfactual_chemo_dosage = (
                    previous_chemo_dose * np.exp(-np.log(2) / drug_half_life)
                    + current_chemo_dose
                )

                counterfactual_cancer_volume = factual_cancer_volume[t] * (
                    1
                    + rho * np.log(K / factual_cancer_volume[t])
                    - beta_c * counterfactual_chemo_dosage
                    - (
                        alpha * counterfactual_radio_dosage
                        + beta * counterfactual_radio_dosage**2
                    )
                    + noise[t + 1]
                )

                cancer_volume[test_idx][: t + 2] = np.append(
                    factual_cancer_volume[: t + 1],
                    [counterfactual_cancer_volume],
                )
                chemo_application_point[test_idx][: t + 1] = np.append(
                    factual_chemo_application_point[:t],
                    [counterfactual_chemo_application_point],
                )
                radio_application_point[test_idx][: t + 1] = np.append(
                    factual_radio_application_point[:t],
                    [counterfactual_radio_application_point],
                )
                patient_types_all_trajectories[test_idx] = patient_types[i]
                sequence_lengths[test_idx] = int(t) + 1
                test_idx = test_idx + 1

            if (factual_cancer_volume[t + 1] >= tumour_death_threshold) or recovery_rvs[
                t
            ] <= np.exp(-factual_cancer_volume[t + 1] * tumour_cell_density):
                break

    outputs = {
        "cancer_volume": cancer_volume[:test_idx],
        "chemo_application": chemo_application_point[:test_idx],
        "radio_application": radio_application_point[:test_idx],
        "sequence_lengths": sequence_lengths[:test_idx],
        "patient_types": patient_types_all_trajectories[:test_idx],
    }

    print("Call to simulate counterfactuals data")

    return outputs


def simulate_sequence_test(
    simulation_params,
    num_time_steps,
    projection_horizon,
    treatment_options,
):
    """
    Core routine to generate simulation test paths to asses all of the counterfactuals.

    :param simulation_params:
    :param num_time_steps:
    :param assigned_actions:
    :return:
    """

    np.random.seed(100)

    total_num_radio_treatments = 1
    total_num_chemo_treatments = 1

    radio_amt = np.array([2.0 for i in range(total_num_radio_treatments)])  # Gy
    radio_days = np.array([i + 1 for i in range(total_num_radio_treatments)])
    chemo_amt = [5.0 for i in range(total_num_chemo_treatments)]
    chemo_days = [(i + 1) * 7 for i in range(total_num_chemo_treatments)]

    # sort this
    chemo_idx = np.argsort(chemo_days)
    chemo_amt = np.array(chemo_amt)[chemo_idx]
    chemo_days = np.array(chemo_days)[chemo_idx]

    drug_half_life = 1  # one day half life for drugs

    # Unpack simulation parameters
    initial_stages = simulation_params["initial_stages"]
    initial_volumes = simulation_params["initial_volumes"]
    alphas = simulation_params["alpha"]
    rhos = simulation_params["rho"]
    betas = simulation_params["beta"]
    beta_cs = simulation_params["beta_c"]
    Ks = simulation_params["K"]
    patient_types = simulation_params["patient_types"]
    window_size = simulation_params[
        "window_size"
    ]  # controls the lookback of the treatment assignment policy

    # Coefficients for treatment assignment probabilities
    chemo_sigmoid_intercepts = simulation_params["chemo_sigmoid_intercepts"]
    radio_sigmoid_intercepts = simulation_params["radio_sigmoid_intercepts"]
    chemo_sigmoid_betas = simulation_params["chemo_sigmoid_betas"]
    radio_sigmoid_betas = simulation_params["radio_sigmoid_betas"]

    num_patients = initial_stages.shape[0]

    num_test_points = 2 * num_patients * num_time_steps * projection_horizon

    # Commence Simulation
    cancer_volume = np.zeros((num_test_points, num_time_steps + projection_horizon))
    chemo_application_point = np.zeros(
        (num_test_points, num_time_steps + projection_horizon),
    )
    radio_application_point = np.zeros(
        (num_test_points, num_time_steps + projection_horizon),
    )
    sequence_lengths = np.zeros(num_test_points)
    patient_types_all_trajectories = np.zeros(num_test_points)
    patient_ids_all_trajectories = np.zeros(num_test_points)
    patient_current_t = np.zeros(num_test_points)

    test_idx = 0

    # Run actual simulation
    for i in range(num_patients):

        if i % 200 == 0:
            logging.info("Simulating patient {} of {}".format(i, num_patients))

        noise = 0.01 * np.random.randn(num_time_steps + 20)  # 5% cell variability
        recovery_rvs = np.random.rand(num_time_steps)

        # initial values
        factual_cancer_volume = np.zeros(num_time_steps)
        factual_chemo_dosage = np.zeros(num_time_steps)
        factual_radio_dosage = np.zeros(num_time_steps)
        factual_chemo_application_point = np.zeros(num_time_steps)
        factual_radio_application_point = np.zeros(num_time_steps)
        factual_chemo_probabilities = np.zeros(num_time_steps)
        factual_radio_probabilities = np.zeros(num_time_steps)

        chemo_application_rvs = np.random.rand(num_time_steps)
        radio_application_rvs = np.random.rand(num_time_steps)

        factual_cancer_volume[0] = initial_volumes[i]

        alpha = alphas[i]
        beta = betas[i]
        beta_c = beta_cs[i]
        rho = rhos[i]
        K = Ks[i]

        for t in range(0, num_time_steps - 1):

            # Factual treatments and outcomes
            current_chemo_dose = 0.0
            previous_chemo_dose = 0.0 if t == 0 else factual_chemo_dosage[t - 1]

            # Action probabilities + death or recovery simulations
            cancer_volume_used = cancer_volume[i, max(t - window_size, 0) : t + 1]
            cancer_diameter_used = np.array(
                [calc_diameter(vol) for vol in cancer_volume_used],
            ).mean()  # mean diameter over 15 days
            cancer_metric_used = cancer_diameter_used

            # probabilities
            radio_prob = 1.0 / (
                1.0
                + np.exp(
                    -radio_sigmoid_betas[i]
                    * (cancer_metric_used - radio_sigmoid_intercepts[i]),
                )
            )
            chemo_prob = 1.0 / (
                1.0
                + np.exp(
                    -chemo_sigmoid_betas[i]
                    * (cancer_metric_used - chemo_sigmoid_intercepts[i]),
                )
            )

            factual_chemo_probabilities[t] = chemo_prob
            factual_radio_probabilities[t] = radio_prob

            # Action application
            if radio_application_rvs[t] < radio_prob:
                factual_radio_application_point[t] = 1
                factual_radio_dosage[t] = radio_amt[0]

            if chemo_application_rvs[t] < chemo_prob:
                factual_chemo_application_point[t] = 1
                current_chemo_dose = chemo_amt[0]

            # Update chemo dosage
            factual_chemo_dosage[t] = (
                previous_chemo_dose * np.exp(-np.log(2) / drug_half_life)
                + current_chemo_dose
            )

            # Factual treatments and outcomes
            factual_cancer_volume[t + 1] = factual_cancer_volume[t] * (
                1
                + rho * np.log(K / factual_cancer_volume[t])
                - beta_c * factual_chemo_dosage[t]
                - (
                    alpha * factual_radio_dosage[t]
                    + beta * factual_radio_dosage[t] ** 2
                )
                + noise[t + 1]
            )  # add noise to fit residuals

            factual_cancer_volume[t + 1] = np.clip(
                factual_cancer_volume[t + 1],
                0,
                tumour_death_threshold,
            )

            for treatment_option in treatment_options:

                counterfactual_cancer_volume = np.zeros(
                    shape=(t + 1 + projection_horizon + 1),
                )
                counterfactual_chemo_application_point = np.zeros(
                    shape=(t + 1 + projection_horizon),
                )
                counterfactual_radio_application_point = np.zeros(
                    shape=(t + 1 + projection_horizon),
                )
                counterfactual_chemo_dosage = np.zeros(
                    shape=(t + 1 + projection_horizon),
                )
                counterfactual_radio_dosage = np.zeros(
                    shape=(t + 1 + projection_horizon),
                )

                counterfactual_cancer_volume[: t + 2] = factual_cancer_volume[: t + 2]
                counterfactual_chemo_application_point[
                    : t + 1
                ] = factual_chemo_application_point[: t + 1]
                counterfactual_radio_application_point[
                    : t + 1
                ] = factual_radio_application_point[: t + 1]
                counterfactual_chemo_dosage[: t + 1] = factual_chemo_dosage[: t + 1]
                counterfactual_radio_dosage[: t + 1] = factual_radio_dosage[: t + 1]

                for projection_time in range(0, projection_horizon):

                    current_t = t + 1 + projection_time
                    previous_chemo_dose = counterfactual_chemo_dosage[current_t - 1]

                    current_chemo_dose = 0.0
                    counterfactual_radio_dosage[current_t] = 0.0
                    if treatment_option[projection_time][0] == 1:
                        counterfactual_chemo_application_point[current_t] = 1
                        current_chemo_dose = chemo_amt[0]

                    if treatment_option[projection_time][1] == 1:
                        counterfactual_radio_application_point[current_t] = 1
                        counterfactual_radio_dosage[current_t] = radio_amt[0]

                    counterfactual_chemo_dosage[current_t] = (
                        previous_chemo_dose * np.exp(-np.log(2) / drug_half_life)
                        + current_chemo_dose
                    )

                    counterfactual_cancer_volume[
                        current_t + 1
                    ] = counterfactual_cancer_volume[current_t] * (
                        1
                        + rho
                        * np.log(
                            K / (counterfactual_cancer_volume[current_t] + 1e-07)
                            + 1e-07,
                        )
                        - beta_c * counterfactual_chemo_dosage[current_t]
                        - (
                            alpha * counterfactual_radio_dosage[current_t]
                            + beta * counterfactual_radio_dosage[current_t] ** 2
                        )
                        + noise[current_t + 1]
                    )

                if np.isnan(counterfactual_cancer_volume).any():
                    continue

                cancer_volume[test_idx][
                    : t + 1 + projection_horizon + 1
                ] = counterfactual_cancer_volume
                chemo_application_point[test_idx][
                    : t + 1 + projection_horizon
                ] = counterfactual_chemo_application_point
                radio_application_point[test_idx][
                    : t + 1 + projection_horizon
                ] = counterfactual_radio_application_point
                patient_types_all_trajectories[test_idx] = patient_types[i]
                patient_ids_all_trajectories[test_idx] = i
                patient_current_t[test_idx] = t

                sequence_lengths[test_idx] = int(t) + 2
                test_idx = test_idx + 1

            if (factual_cancer_volume[t + 1] >= tumour_death_threshold) or recovery_rvs[
                t
            ] <= np.exp(-factual_cancer_volume[t + 1] * tumour_cell_density):
                break

    outputs = {
        "cancer_volume": cancer_volume[:test_idx],
        "chemo_application": chemo_application_point[:test_idx],
        "radio_application": radio_application_point[:test_idx],
        "sequence_lengths": sequence_lengths[:test_idx],
        "patient_types": patient_types_all_trajectories[:test_idx],
        "patient_ids_all_trajectories": patient_ids_all_trajectories[:test_idx],
        "patient_current_t": patient_current_t[:test_idx],
    }

    print("Call to simulate counterfactuals data")

    return outputs


def get_scaling_params(sim):
    real_idx = ["cancer_volume", "chemo_dosage", "radio_dosage"]

    # df = pd.DataFrame({k: sim[k] for k in real_idx})
    means = {}
    stds = {}
    seq_lengths = sim["sequence_lengths"]
    for k in real_idx:
        active_values = []
        for i in range(seq_lengths.shape[0]):
            end = int(seq_lengths[i])
            active_values += list(sim[k][i, :end])

        means[k] = np.mean(active_values)
        stds[k] = np.std(active_values)

    # Add means for static variables`
    means["patient_types"] = np.mean(sim["patient_types"])
    stds["patient_types"] = np.std(sim["patient_types"])

    return pd.Series(means), pd.Series(stds)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Plotting Functions


def plot_treatments(patient):
    df = pd.DataFrame(
        {
            "N(t)": outputs["cancer_volume"][patient],
            "C(t)": outputs["chemo_application"][patient],
            "d(t)": outputs["radio_application"][patient],
        },
    )
    df = df[["N(t)", "C(t)", "d(t)"]]
    df.plot(secondary_y=["C(t)", "d(t)"])
    plt.xlabel("$t$")
    plt.show()


def get_cancer_sim_data(
    chemo_coeff,
    radio_coeff,
    b_load,
    b_save=False,
    seed=100,
    model_root="results",
    window_size=15,
):
    """
    这里chemo_coeff和radio_coeff都是预先设定好的关于施加treatment的参数
    """
    if window_size == 15:
        pickle_file = os.path.join(
            model_root,
            "new_cancer_sim_{}_{}.p".format(chemo_coeff, radio_coeff),
        )
    else:
        pickle_file = os.path.join(
            model_root,
            "new_cancer_sim_{}_{}_{}.p".format(chemo_coeff, radio_coeff, window_size),
        )

    def _generate():
        num_time_steps = 60  # about half a year
        np.random.seed(seed)
        num_patients = 10000

        params = get_confounding_params(
            num_patients,
            chemo_coeff=chemo_coeff,
            radio_coeff=radio_coeff,
        )  # 这一步只是得到parameters，当然parameters本身是可以对于每个病人的
        params["window_size"] = window_size
        training_data = simulate(params, num_time_steps)

        params = get_confounding_params(
            int(num_patients / 10),
            chemo_coeff=chemo_coeff,
            radio_coeff=radio_coeff,
        )
        params["window_size"] = window_size
        validation_data = simulate(params, num_time_steps)

        params = get_confounding_params(
            int(num_patients / 10),
            chemo_coeff=chemo_coeff,
            radio_coeff=radio_coeff,
        )
        params["window_size"] = window_size
        test_data_factuals = simulate(params, num_time_steps)
        # 到这里为止，就是正常的每个病人有各自不同的treatment，当然也有各自不同的tumor v
        test_data_counterfactuals = simulate_counterfactual_test_data(
            params,
            num_time_steps,
        )

        params = get_confounding_params(
            int(num_patients / 10),
            chemo_coeff=chemo_coeff,
            radio_coeff=radio_coeff,
        )
        params["window_size"] = window_size
        treatment_options = np.array(
            [
                [(1, 0), (0, 0), (0, 0), (0, 0), (0, 0)],
                [(0, 0), (1, 0), (0, 0), (0, 0), (0, 0)],
                [(0, 0), (0, 0), (1, 0), (0, 0), (0, 0)],
                [(0, 0), (0, 0), (0, 0), (1, 0), (0, 0)],
                [(0, 0), (0, 0), (0, 0), (0, 0), (1, 0)],
                [(0, 1), (0, 0), (0, 0), (0, 0), (0, 0)],
                [(0, 0), (0, 1), (0, 0), (0, 0), (0, 0)],
                [(0, 0), (0, 0), (0, 1), (0, 0), (0, 0)],
                [(0, 0), (0, 0), (0, 0), (0, 1), (0, 0)],
                [(0, 0), (0, 0), (0, 0), (0, 0), (0, 1)],
            ],
        )
        test_data_seq = simulate_sequence_test(
            params,
            num_time_steps,
            5,
            treatment_options,
        )
        # 这个地方和之前的区别就在于，这里考虑了未来5个时间点的情况。也就是说正常counterfactual只考虑了当前各种不同的treatment
        # 而sequence则考虑了未来5个时间点，相当于就是未来一系列的治疗方案，不是一个点
        # 但是问题在于，这里只有5个时间点，是不是还是太过于局限了，并且，仅仅考虑了放疗/化疗的情况，并没有二者都有，并且未来5个点只有一个放疗/化疗

        scaling_data = get_scaling_params(training_data)
        # 从训练数据得到scaling数据，用于后续的数据标准化

        pickle_map = {
            "chemo_coeff": chemo_coeff,
            "radio_coeff": radio_coeff,
            "num_time_steps": num_time_steps,
            "training_data": training_data,
            "validation_data": validation_data,
            "test_data": test_data_counterfactuals,
            "test_data_factuals": test_data_factuals,
            "test_data_seq": test_data_seq,
            "scaling_data": scaling_data,
            "window_size": window_size,
        }

        if b_save:
            logging.info("Saving pickle map to {}".format(pickle_file))
            pickle.dump(pickle_map, open(pickle_file, "wb"))
        return pickle_map

    # Controls whether to regenerate the data, or load from a persisted file
    if not b_load:
        pickle_map = _generate()

    else:
        logging.info("Loading pickle map from {}".format(pickle_file))

        try:
            pickle_map = pickle.load(open(pickle_file, "rb"))

        except IOError:
            logging.info(
                "Pickle file does not exist, regenerating: {}".format(pickle_file),
            )
            pickle_map = _generate()

    return pickle_map


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Run Simulation


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.INFO)

    np.random.seed(100)

    num_time_steps = 60  # 6 month followup
    num_patients = 200

    simulation_params = get_confounding_params(
        num_patients,
        chemo_coeff=5.0,
        radio_coeff=5.0,
    )
    simulation_params["window_size"] = 15

    projection_horizon = 5
    treatment_options = np.array(
        [
            [(1, 0), (0, 0), (0, 1), (0, 0), (0, 0)],
            [(0, 0), (1, 0), (0, 1), (0, 0), (0, 0)],
        ],
    )

    outputs = simulate(simulation_params, num_time_steps)

    print(outputs["cancer_volume"][:10])
    print(outputs["chemo_probabilities"][:10])
    print(outputs["radio_probabilities"][:10])

    print("finished")
