# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A PPO Agent.

Implements the PPO algorithm from (Schulman, 2017):
https://arxiv.org/abs/1707.06347

PPO is a simplification of the TRPO algorithm, both of which add stability to
policy gradient RL, while allowing multiple updates per batch of on-policy data,
by limiting the KL divergence between the policy that sampled the data and the
updated policy.

TRPO enforces a hard optimization constraint, but is a complex algorithm, which
often makes it harder to use in practice. PPO approximates the effect of TRPO
by using a soft constraint. There are two methods presented in the paper for
implementing the soft constraint: an adaptive KL loss penalty, and
limiting the objective value based on a clipped version of the policy importance
ratio. This code implements both, and allows the user to use either method or
both by modifying hyperparameters.

The importance ratio clipping is described in eq (7) and the adaptive KL penatly
is described in eq (8) of https://arxiv.org/pdf/1707.06347.pdf
- To disable IR clipping, set the importance_ratio_clipping parameter to 0.0
- To disable the adaptive KL penalty, set the initial_adaptive_kl_beta parameter
  to 0.0
- To disable the fixed KL cutoff penalty, set the kl_cutoff_factor parameter
  to 0.0

In order to compute KL divergence, the replay buffer must store action
distribution parameters from data collection. For now, it is assumed that
continuous actions are represented by a Normal distribution with mean & stddev,
and discrete actions are represented by a Categorical distribution with logits.

Note that the objective function chooses the lower value of the clipped and
unclipped objectives. Thus, if the importance ratio exceeds the clipped bounds,
then the optimizer will still not be incentivized to pass the bounds, as it is
only optimizing the minimum.

Advantage is computed using Generalized Advantage Estimation (GAE):
https://arxiv.org/abs/1506.02438
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from tf_agents.agents import tf_agent
from tf_agents.agents.ppo import ppo_policy
from tf_agents.agents.ppo import ppo_utils
from tf_agents.environments import trajectory
from tf_agents.policies import greedy_policy
from tf_agents.policies import policy_step
from tf_agents.specs import tensor_spec
from tf_agents.utils import eager_utils
from tf_agents.utils import nest_utils
from tf_agents.utils import tensor_normalizer
import tf_agents.utils.common as common_utils
import gin

nest = tf.contrib.framework.nest


def _normalize_advantages(advantages, axes=(0,), variance_epsilon=1e-8):
  adv_mean, adv_var = tf.nn.moments(advantages, axes=axes, keep_dims=True)
  normalized_advantages = (
      (advantages - adv_mean) / (tf.sqrt(adv_var) + variance_epsilon))
  return normalized_advantages


@gin.configurable
class PPOAgent(tf_agent.Base):
  """A PPO Agent."""

  def __init__(self,
               time_step_spec,
               action_spec,
               optimizer=None,
               actor_net=None,
               value_net=None,
               importance_ratio_clipping=0.0,
               lambda_value=0.95,
               discount_factor=0.99,
               entropy_regularization=0.0,
               policy_l2_reg=0.0,
               value_function_l2_reg=0.0,
               value_pred_loss_coef=0.5,
               num_epochs=25,
               use_gae=False,
               use_td_lambda_return=False,
               normalize_rewards=True,
               reward_norm_clipping=10.0,
               normalize_observations=True,
               log_prob_clipping=0.0,
               kl_cutoff_factor=2.0,
               kl_cutoff_coef=1000.0,
               initial_adaptive_kl_beta=1.0,
               adaptive_kl_target=0.01,
               adaptive_kl_tolerance=0.3,
               gradient_clipping=None,
               check_numerics=False,
               debug_summaries=False,
               summarize_grads_and_vars=False):
    """Creates a PPO Agent.

    Args:
      time_step_spec: A `TimeStep` spec of the expected time_steps.
      action_spec: A nest of BoundedTensorSpec representing the actions.
      optimizer: Optimizer to use for the agent.
      actor_net: A function actor_net(observations, action_spec) that returns
        tensor of action distribution params for each observation. Takes nested
        observation and returns nested action.
      value_net: A function value_net(time_steps) that returns value tensor
        from neural net predictions for each observation. Takes nested
        observation and returns batch of value_preds.
      importance_ratio_clipping: Epsilon in clipped, surrogate PPO objective.
        For more detail, see explanation at the top of the doc.
      lambda_value: Lambda parameter for TD-lambda computation.
      discount_factor: Discount factor for return computation.
      entropy_regularization: Coefficient for entropy regularization loss term.
      policy_l2_reg: Coefficient for l2 regularization of policy weights.
      value_function_l2_reg: Coefficient for l2 regularization of value function
        weights.
      value_pred_loss_coef: Multiplier for value prediction loss to balance
        with policy gradient loss.
      num_epochs: Number of epochs for computing policy updates.
      use_gae: If True (default False), uses generalized advantage estimation
        for computing per-timestep advantage. Else, just subtracts value
        predictions from empirical return.
      use_td_lambda_return: If True (default False), uses td_lambda_return for
        training value function.
        (td_lambda_return = gae_advantage + value_predictions)
      normalize_rewards: If true, keeps moving variance of rewards and
        normalizes incoming rewards.
      reward_norm_clipping: Value above an below to clip normalized reward.
      normalize_observations: If true, keeps moving mean and variance of
        observations and normalizes incoming observations.
      log_prob_clipping: +/- value for clipping log probs to prevent inf / NaN
        values.  Default: no clipping.
      kl_cutoff_factor: If policy KL changes more than this much for any single
        timestep, adds a squared KL penalty to loss function.
      kl_cutoff_coef: Loss coefficient for kl cutoff term.
      initial_adaptive_kl_beta: Initial value for beta coefficient of adaptive
        kl penalty.
      adaptive_kl_target: Desired kl target for policy updates. If actual kl is
        far from this target, adaptive_kl_beta will be updated.
      adaptive_kl_tolerance: A tolerance for adaptive_kl_beta. Mean KL above
        (1 + tol) * adaptive_kl_target, or below (1 - tol) * adaptive_kl_target,
        will cause adaptive_kl_beta to be updated.
      gradient_clipping: Norm length to clip gradients.  Default: no clipping.
      check_numerics: If true, adds tf.check_numerics to help find NaN / Inf
        values. For debugging only.
      debug_summaries: A bool to gather debug summaries.
      summarize_grads_and_vars: If true, gradient summaries will be written.
    """
    super(PPOAgent, self).__init__(time_step_spec, action_spec)
    self._importance_ratio_clipping = importance_ratio_clipping
    self._lambda = lambda_value
    self._discount_factor = discount_factor
    self._policy_l2_reg = policy_l2_reg
    self._value_function_l2_reg = value_function_l2_reg
    self._entropy_regularization = entropy_regularization
    self._value_pred_loss_coef = value_pred_loss_coef
    self._use_gae = use_gae
    self._use_td_lambda_return = use_td_lambda_return
    self._num_epochs = num_epochs
    self._log_prob_clipping = log_prob_clipping
    self._gradient_clipping = gradient_clipping or 0.0
    self._kl_cutoff_factor = kl_cutoff_factor
    self._kl_cutoff_coef = kl_cutoff_coef
    if initial_adaptive_kl_beta > 0.0:
      # TODO(kbanoop): Rename create_variable.
      self._adaptive_kl_beta = common_utils.create_counter(
          'adaptive_kl_beta', initial_adaptive_kl_beta, dtype=tf.float32)
    else:
      self._adaptive_kl_beta = None
    self._adaptive_kl_target = adaptive_kl_target
    self._adaptive_kl_tolerance = adaptive_kl_tolerance
    self._check_numerics = check_numerics
    self._debug_summaries = debug_summaries
    self._summarize_grads_and_vars = summarize_grads_and_vars

    self._reward_norm_clipping = reward_norm_clipping
    self._reward_normalizer = None
    if normalize_rewards:
      self._reward_normalizer = tensor_normalizer.StreamingTensorNormalizer(
          tensor_spec.TensorSpec([], tf.float32), scope='normalize_reward')

    self._observation_normalizer = None
    if normalize_observations:
      self._observation_normalizer = (
          tensor_normalizer.StreamingTensorNormalizer(
              time_step_spec.observation, scope='normalize_observations'))

    self._optimizer = optimizer

    self._actor_net = actor_net
    self._value_net = value_net

    # TODO(oars): Fix uses of policy_state, right now code assumes ppo_policy
    # only returns 1 state, and that actor and value networks have the same
    # state.
    self._policy_state_spec = self._actor_net.state_spec
    self._policy = self.collect_policy()
    self._action_distribution_class_spec = (
        ppo_utils.get_distribution_class_spec(self._policy,
                                              self.time_step_spec()))

  def _make_policy(self, collect):
    return ppo_policy.PPOPolicy(
        time_step_spec=self.time_step_spec(),
        action_spec=self.action_spec(),
        policy_state_spec=self.policy_state_spec(),
        actor_network=self._actor_net,
        value_network=self._value_net,
        observation_normalizer=self._observation_normalizer,
        clip=False,
        collect=collect)

  def _make_ppo_trajectory_spec(self, action_distribution_params_spec):
    # Make policy_step_spec with action_spec, empty tuple for policy_state, and
    # (act_log_prob_spec, value_pred_spec, action_distribution_params_spec) for
    # info.
    # TODO(eholly): Get policy_step_spec from policy.
    policy_step_spec = policy_step.PolicyStep(
        action=self.action_spec(), state=self.policy_state_spec(),
        info=action_distribution_params_spec)
    trajectory_spec = trajectory.from_transition(
        self.time_step_spec(), policy_step_spec, self.time_step_spec())
    return trajectory_spec

  def collect_data_spec(self):
    """Returns a `Trajectory` spec, as expected by the `collect_policy`.

    Returns:
      A `Trajectory` spec.
    """
    action_distribution_params_spec = ppo_utils.get_distribution_params_spec(
        self._policy,
        self.time_step_spec())
    return self._make_ppo_trajectory_spec(action_distribution_params_spec)

  def policy_state_spec(self):
    """TensorSpec describing the policy_state.

    Returns:
      An single TensorSpec, or a nested dict, list or tuple of
      `TensorSpec` objects, which describe the shape and
      dtype of each Tensor in policy_state.
    """
    return self._policy_state_spec

  @property
  def actor_net(self):
    """Returns actor_net TensorFlow template function."""
    return self._actor_net

  def initialize(self):
    """Returns an op to initialize the agent. tf.no_op() for this agent.

    Returns:
      tf.no_op() for this agent.
    """
    return tf.no_op()

  def policy(self):
    """Return the current policy held by the agent.

    Returns:
      A subclass of tf_policy.Base.
    """
    return greedy_policy.GreedyPolicy(self._make_policy(collect=False))

  def collect_policy(self):
    """Returns a policy for collecting data from the environment.

    Returns:
      A tf_policy.Base object.
    """
    return self._make_policy(collect=True)

  def compute_returns(self,
                      rewards,
                      discounts,
                      norm_variance_epsilon=1e6):
    """Compute the Monte Carlo return from each index in an episode.

    Args:
      rewards: Tensor of per-timestep reward in the episode.
      discounts: Tensor of per-timestep discount factor. Should be 0 for final
        step of each episode.
      norm_variance_epsilon: Variance epsilon to use when normalizing returns.

    Returns:
      Tensor of per-timestep cumulative returns.
    """
    rewards.shape.assert_is_compatible_with(discounts.shape)
    check_shape = tf.assert_equal(
        tf.shape(rewards), tf.shape(discounts),
        message='rewards should have the same shape as discounts.')
    with tf.control_dependencies([check_shape]):
      # Transpose so that scan operates on time dimension.
      rewards, discounts = tf.transpose(rewards), tf.transpose(discounts)

    def discounted_accumulate_rewards(next_step_return, reward_and_discount):
      reward, discount = reward_and_discount
      return next_step_return * discount + reward

    # Cumulatively sum discounted reward.
    returns = tf.scan(discounted_accumulate_rewards,
                      [rewards, discounts],
                      reverse=True,
                      initializer=tf.zeros_like(rewards[0]))
    returns = tf.transpose(returns)

    return returns

  def compute_advantages(self,
                         rewards,
                         returns,
                         discounts,
                         value_preds):
    """Compute advantages, optionally using GAE.

    Based on baselines ppo1 implementation. Removes final timestep, as it needs
    to use this timestep for next-step value prediction for TD error
    computation.

    Args:
      rewards: Tensor of per-timestep rewards.
      returns: Tensor of per-timestep returns.
      discounts: Tensor of per-timestep discounts. Zero for terminal timesteps.
      value_preds: Cached value estimates from the data-collection policy.

    Returns:
      advantages: Tensor of length (len(rewards) - 1), because the final
        timestep is just used for next-step value prediction.
    """
    # Arg value_preds was appended with final next_step value. Make tensors
    #   next_value_preds by stripping first and last elements respectively.
    next_value_preds = value_preds[:, 1:]
    value_preds = value_preds[:, :-1]

    if not self._use_gae:
      with tf.name_scope('empirical_advantage'):
        advantages = returns - value_preds
    else:
      with tf.name_scope('generalized_advantage_estimation'):
        deltas = rewards + discounts * next_value_preds - value_preds

        # Transpose so that scan will operate over time dimension.
        deltas, discounts = tf.transpose(deltas), tf.transpose(discounts)

        def gae_step(next_step_val, delta_and_discount):
          delta, discount = delta_and_discount
          return delta + discount * next_step_val * self._lambda

        advantages = tf.scan(gae_step,
                             [deltas, discounts],
                             reverse=True,
                             initializer=tf.zeros_like(deltas[0]))

        # Undo transpose.
        advantages = tf.transpose(advantages)

    return advantages

  def build_train_op(self,
                     time_steps,
                     actions,
                     act_log_probs,
                     returns,
                     normalized_advantages,
                     action_distribution_parameters,
                     valid_mask,
                     train_step,
                     summarize_gradients,
                     gradient_clipping,
                     debug_summaries):
    """Compute the loss and create optimization op for one training epoch.

    All tensors should have a single batch dimension.

    Args:
      time_steps: A minibatch of TimeStep tuples.
      actions: A minibatch of actions.
      act_log_probs: A minibatch of action probabilities (probability under the
        sampling policy).
      returns: A minibatch of per-timestep returns.
      normalized_advantages: A minibatch of normalized per-timestep advantages.
      action_distribution_parameters: Parameters of data-collecting action
        distribution. Needed for KL computation.
      valid_mask: Mask for invalid timesteps. Float value 1.0 for valid
        timesteps and 0.0 for invalid timesteps. (Timesteps which either are
        betweeen two episodes, or part of an unfinished episode at the end of
        one batch dimension.)
      train_step: A train_step variable to increment for each train step.
        Typically the global_step.
      summarize_gradients: If true, gradient summaries will be written.
      gradient_clipping: Norm length to clip gradients.
      debug_summaries: True if debug summaries should be created.
    Returns:
      train_op: An op that runs training with this batch of data.
      losses: A list of policy_gradient_loss, value_estimation_loss,
        l2_regularization_loss, and entropy_regularization_loss.
    """
    # Evaluate the current policy on timesteps.

    # batch_size from time_steps
    batch_size = nest_utils.get_outer_shape(time_steps, self._time_step_spec)[0]
    policy_state = self._policy.get_initial_state(batch_size)
    distribution_step = self._policy.distribution(time_steps, policy_state)
    # TODO(eholly): Rename policy distributions to something clear and uniform.
    current_policy_distribution = distribution_step.action

    # Call all loss functions and add all loss values.
    value_estimation_loss = self.value_estimation_loss(
        time_steps, returns, valid_mask, debug_summaries)
    policy_gradient_loss = self.policy_gradient_loss(
        time_steps,
        actions,
        tf.stop_gradient(act_log_probs),
        tf.stop_gradient(normalized_advantages),
        current_policy_distribution,
        valid_mask,
        debug_summaries=debug_summaries)

    if self._policy_l2_reg > 0.0 or self._value_function_l2_reg > 0.0:
      l2_regularization_loss = self.l2_regularization_loss(debug_summaries)
    else:
      l2_regularization_loss = tf.zeros_like(policy_gradient_loss)

    if self._entropy_regularization > 0.0:
      entropy_regularization_loss = self.entropy_regularization_loss(
          time_steps, current_policy_distribution, valid_mask, debug_summaries)
    else:
      entropy_regularization_loss = tf.zeros_like(policy_gradient_loss)

    kl_penalty_loss = self.kl_penalty_loss(time_steps,
                                           action_distribution_parameters,
                                           current_policy_distribution,
                                           valid_mask,
                                           debug_summaries)

    total_loss = (policy_gradient_loss +
                  value_estimation_loss +
                  l2_regularization_loss +
                  entropy_regularization_loss +
                  kl_penalty_loss)

    clip_gradients = (tf.contrib.training.clip_gradient_norms_fn(
        gradient_clipping) if gradient_clipping > 0 else lambda x: x)

    # If summarize_gradients, create functions for summarizing both gradients
    # and variables.
    if summarize_gradients and debug_summaries:
      def _create_summaries(grads_and_vars):
        grads_and_vars = eager_utils.add_gradients_summaries(grads_and_vars)
        grads_and_vars = eager_utils.add_variables_summaries(grads_and_vars)
        grads_and_vars = clip_gradients(grads_and_vars)
        return grads_and_vars
      transform_grads_fn = _create_summaries
    else:
      transform_grads_fn = clip_gradients

    train_op = tf.contrib.training.create_train_op(
        total_loss,
        self._optimizer,
        global_step=train_step,
        transform_grads_fn=transform_grads_fn,
        variables_to_train=(self._actor_net.trainable_weights +
                            self._value_net.trainable_weights))

    return train_op, [policy_gradient_loss, value_estimation_loss,
                      l2_regularization_loss, entropy_regularization_loss,
                      kl_penalty_loss]

  def compute_return_and_advantage(self, time_steps, actions, next_time_steps,
                                   value_preds):
    """Compute the Monte Carlo return and advantage.

    Normalazation will be applied to the computed returns and advantages if
    it's enabled.

    Args:
      time_steps: batched tensor of TimeStep tuples before action is taken.
      actions: batched tensor of actions.
      next_time_steps: batched tensor of TimeStep tuples after action is taken.
      value_preds: Batched value predction tensor. Should have one more entry in
        time index than time_steps, with the final value corresponding to the
        value prediction of the final state.

    Returns:
      tuple of (return, normalized_advantage), both are batched tensors.
    """
    discounts = next_time_steps.discount * tf.constant(self._discount_factor,
                                                       dtype=tf.float32)

    rewards = next_time_steps.reward
    if self._debug_summaries:
      tf.contrib.summary.histogram('actions', actions)
      # Summarize rewards before they get normalized below.
      tf.contrib.summary.histogram('rewards', rewards)

    # Normalize rewards if self._reward_normalizer is defined.
    if self._reward_normalizer:
      rewards = self._reward_normalizer.normalize(
          rewards, center_mean=False, clip_value=self._reward_norm_clipping)
      if self._debug_summaries:
        tf.contrib.summary.histogram('rewards_normalized', rewards)

    # Make discount 0.0 at end of each episode to restart cumulative sum
    #   end of each episode.
    episode_mask = common_utils.get_episode_mask(next_time_steps)
    discounts *= episode_mask

    # Compute Monte Carlo returns.
    returns = self.compute_returns(rewards, discounts)
    if self._debug_summaries:
      tf.contrib.summary.histogram('returns', returns)

    # Compute advantages.
    advantages = self.compute_advantages(rewards, returns,
                                         discounts, value_preds)
    normalized_advantages = _normalize_advantages(advantages, axes=(0, 1))
    if self._debug_summaries:
      tf.contrib.summary.histogram('advantages', advantages)
      tf.contrib.summary.histogram('advantages_normalized',
                                   normalized_advantages)

    # Return TD-Lambda returns if both use_td_lambda_return and use_gae.
    if self._use_td_lambda_return:
      if not self._use_gae:
        tf.logging.warning('use_td_lambda_return was True, but use_gae was '
                           'False. Using Monte Carlo return.')
      else:
        returns = tf.add(advantages, value_preds, name='td_lambda_returns')

    return returns, normalized_advantages

  @gin.configurable(module='tf_agents.agents.ppo.ppo_agent.PPOAgent')
  def train(self, experience, train_step_counter=None):
    """Update the agent estimates given a batch of experience.

    Args:
      experience: Trajectory of experience to train on.
      train_step_counter: An optional variable to increment for each train step.
        Typically the global_step.
    Returns:
      A train_op to train the actor and critic networks.
    Raises:
      ValueError: If replay_buffer is None, and the agent does not have an
        internal replay buffer.
    """
    train_op = self.train_from_experience(
        experience=experience,
        train_step_counter=train_step_counter)

    return train_op

  def train_from_experience(self,
                            experience,
                            train_step_counter=None):
    """Update the agent estimates given a batch of experience.

    Args:
      experience: A `trajectory.Trajectory` representing training data.
      train_step_counter: An optional variable to increment for each train step.
        Typically the global_step.

    Returns:
      A train_op to train the actor and critic networks.
    Raises:
      ValueError: If replay_buffer is None, and the agent does not have an
        internal replay buffer.
    """
    # Change trajectory to transitions.
    trajectory0 = nest.map_structure(lambda t: t[:, :-1], experience)
    trajectory1 = nest.map_structure(lambda t: t[:, 1:], experience)

    # Get individual tensors from transitions.
    (time_steps, policy_steps_, next_time_steps) = trajectory.to_transition(
        trajectory0, trajectory1)
    actions = policy_steps_.action
    action_distribution_parameters = policy_steps_.info

    # Reconstruct per-timestep policy distribution from stored distribution
    #   parameters.
    old_actions_distribution = (
        ppo_utils.get_distribution_from_params_and_classes(
            action_distribution_parameters,
            self._action_distribution_class_spec))

    # Compute log probability of actions taken during data collection, using the
    #   collect policy distribution.
    act_log_probs = common_utils.log_probability(
        old_actions_distribution, actions, self._action_spec)

    # Compute the value predictions for states using the current value function.
    # To be used for return & advantage computation.
    batch_size = nest_utils.get_outer_shape(time_steps, self._time_step_spec)[0]
    policy_state = self._policy.get_initial_state(batch_size=batch_size)

    value_preds, unused_policy_state = self._policy.apply_value_network(
        experience.observation, experience.step_type, policy_state=policy_state)
    value_preds = tf.stop_gradient(value_preds)

    valid_mask = ppo_utils.make_timestep_mask(next_time_steps)

    returns, normalized_advantages = self.compute_return_and_advantage(
        time_steps, actions, next_time_steps, value_preds)

    # Loss tensors across batches will be aggregated for summaries.
    policy_gradient_losses = []
    value_estimation_losses = []
    l2_regularization_losses = []
    entropy_regularization_losses = []
    kl_penalty_losses = []

    # For each epoch, create its own train op that depends on the previous one.
    last_train_op = tf.no_op()
    for i_epoch in range(self._num_epochs):
      with tf.name_scope('epoch_%d' % i_epoch):
        with tf.control_dependencies([last_train_op]):
          # Only save debug summaries for first and last epochs.
          debug_summaries = (self._debug_summaries and
                             (i_epoch == 0 or i_epoch == self._num_epochs-1))

          # Build one epoch train op.
          last_train_op, losses = self.build_train_op(
              time_steps, actions, act_log_probs, returns,
              normalized_advantages, action_distribution_parameters, valid_mask,
              train_step_counter,
              self._summarize_grads_and_vars, self._gradient_clipping,
              debug_summaries)
          policy_gradient_losses.append(losses[0])
          value_estimation_losses.append(losses[1])
          l2_regularization_losses.append(losses[2])
          entropy_regularization_losses.append(losses[3])
          kl_penalty_losses.append(losses[4])

    # After update epochs, update adaptive kl beta, then update observation
    #   normalizer and reward normalizer.
    with tf.control_dependencies([last_train_op]):
      # Compute the mean kl from old.
      batch_size = nest_utils.get_outer_shape(
          time_steps, self._time_step_spec)[0]
      policy_state = self._policy.get_initial_state(batch_size)
      kl_divergence = self._kl_divergence(
          time_steps, action_distribution_parameters,
          self._policy.distribution(time_steps, policy_state).action)
      update_adaptive_kl_beta_op = self.update_adaptive_kl_beta(kl_divergence)

    with tf.control_dependencies([update_adaptive_kl_beta_op]):
      if self._observation_normalizer:
        update_obs_norm = (
            self._observation_normalizer.update(
                time_steps.observation, outer_dims=[0, 1]))
      else:
        update_obs_norm = tf.no_op()
      if self._reward_normalizer:
        update_reward_norm = self._reward_normalizer.update(
            next_time_steps.reward, outer_dims=[0, 1])
      else:
        update_reward_norm = tf.no_op()

    with tf.control_dependencies([update_obs_norm, update_reward_norm]):
      last_train_op = tf.identity(last_train_op)

    # Make summaries for total loss across all epochs.
    # The self._*_losses lists will have been populated by
    #   calls to self.build_train_op.
    with tf.name_scope('Losses/'):
      total_policy_gradient_loss = tf.add_n(policy_gradient_losses)
      total_value_estimation_loss = tf.add_n(value_estimation_losses)
      total_l2_regularization_loss = tf.add_n(l2_regularization_losses)
      total_entropy_regularization_loss = tf.add_n(
          entropy_regularization_losses)
      total_kl_penalty_loss = tf.add_n(kl_penalty_losses)
      tf.contrib.summary.scalar('policy_gradient_loss',
                                total_policy_gradient_loss)
      tf.contrib.summary.scalar('value_estimation_loss',
                                total_value_estimation_loss)
      tf.contrib.summary.scalar('l2_regularization_loss',
                                total_l2_regularization_loss)
      if self._entropy_regularization:
        tf.contrib.summary.scalar('entropy_regularization_loss',
                                  total_entropy_regularization_loss)
      tf.contrib.summary.scalar('kl_penalty_loss',
                                total_kl_penalty_loss)

      total_loss = (
          tf.abs(total_policy_gradient_loss) +
          tf.abs(total_value_estimation_loss) +
          tf.abs(total_entropy_regularization_loss) +
          tf.abs(total_l2_regularization_loss) +
          tf.abs(total_kl_penalty_loss))

      tf.contrib.summary.scalar('total_loss', total_loss)

    if self._summarize_grads_and_vars:
      with tf.name_scope('Variables/'):
        all_vars = (self._actor_net.trainable_weights +
                    self._value_net.trainable_weights)
        for var in all_vars:
          tf.contrib.summary.histogram(var.name.replace(':', '_'), var)

    return last_train_op

  def l2_regularization_loss(self, debug_summaries=False):
    if self._policy_l2_reg > 0 or self._value_function_l2_reg > 0:
      with tf.name_scope('l2_regularization'):
        # Regularize policy weights.
        policy_vars_to_l2_regularize = [v for v in
                                        self._actor_net.trainable_weights
                                        if 'kernel' in v.name]
        policy_l2_losses = [tf.reduce_sum(tf.square(v)) * self._policy_l2_reg
                            for v in policy_vars_to_l2_regularize]

        # Regularize value function weights.
        vf_vars_to_l2_regularize = [v for v in
                                    self._value_net.trainable_weights
                                    if 'kernel' in v.name]
        vf_l2_losses = [tf.reduce_sum(tf.square(v)) *
                        self._value_function_l2_reg
                        for v in vf_vars_to_l2_regularize]

        l2_losses = policy_l2_losses + vf_l2_losses
        total_l2_loss = tf.add_n(l2_losses, name='l2_loss')

        if self._check_numerics:
          total_l2_loss = tf.check_numerics(total_l2_loss, 'total_l2_loss')

        if debug_summaries:
          tf.contrib.summary.histogram('l2_loss', total_l2_loss)
    else:
      total_l2_loss = tf.constant(0.0, dtype=tf.float32, name='zero_l2_loss')

    return total_l2_loss

  def entropy_regularization_loss(self, time_steps, current_policy_distribution,
                                  valid_mask, debug_summaries=False):
    """Create regularization loss tensor based on agent parameters."""
    if self._entropy_regularization > 0:
      nest.assert_same_structure(time_steps, self.time_step_spec())
      with tf.name_scope('entropy_regularization'):
        entropy = tf.to_float(common_utils.entropy(
            current_policy_distribution, self.action_spec()))
        entropy_reg_loss = (tf.reduce_mean(-entropy * valid_mask) *
                            self._entropy_regularization)
        if self._check_numerics:
          entropy_reg_loss = tf.check_numerics(entropy_reg_loss,
                                               'entropy_reg_loss')

        if debug_summaries:
          tf.contrib.summary.histogram('entropy_reg_loss', entropy_reg_loss)
    else:
      entropy_reg_loss = tf.constant(0.0, dtype=tf.float32,
                                     name='zero_entropy_reg_loss')

    return entropy_reg_loss

  def value_estimation_loss(self,
                            time_steps,
                            returns,
                            valid_mask,
                            debug_summaries=False):
    """Computes the value estimation loss for actor-critic training.

    All tensors should have a single batch dimension.

    Args:
      time_steps: A batch of timesteps.
      returns: Per-timestep returns for value function to predict. (Should come
        from TD-lambda computation.)
      valid_mask: Mask for invalid timesteps. Float value 1.0 for valid
        timesteps and 0.0 for invalid timesteps. (Timesteps which either are
        betweeen two episodes, or part of an unfinished episode at the end of
        one batch dimension.)
      debug_summaries: True if debug summaries should be created.
    Returns:
      value_estimation_loss: A scalar value_estimation_loss loss.
    """
    observation = time_steps.observation
    if debug_summaries:
      tf.contrib.summary.histogram('observations', observation)

    batch_size = nest_utils.get_outer_shape(time_steps, self._time_step_spec)[0]
    policy_state = self._policy.get_initial_state(batch_size=batch_size)

    value_preds, unused_policy_state = self._policy.apply_value_network(
        time_steps.observation, time_steps.step_type,
        policy_state=policy_state)
    value_estimation_error = tf.squared_difference(
        returns, value_preds) * valid_mask
    value_estimation_loss = (tf.reduce_mean(value_estimation_error) *
                             self._value_pred_loss_coef)
    if debug_summaries:
      tf.contrib.summary.scalar(
          'value_pred_avg', tf.reduce_mean(value_preds))
      tf.contrib.summary.histogram('value_preds', value_preds)
      tf.contrib.summary.histogram('value_estimation_error',
                                   value_estimation_error)

    if self._check_numerics:
      value_estimation_loss = tf.check_numerics(value_estimation_loss,
                                                'value_estimation_loss')

    return value_estimation_loss

  def policy_gradient_loss(self,
                           time_steps,
                           actions,
                           sample_action_log_probs,
                           advantages,
                           current_policy_distribution,
                           valid_mask,
                           debug_summaries=False):
    """Create tensor for policy gradient loss.

    All tensors should have a single batch dimension.

    Args:
      time_steps: TimeSteps with observations for each timestep.
      actions: Tensor of actions for timesteps, aligned on index.
      sample_action_log_probs: Tensor of ample probability of each action.
      advantages: Tensor of advantage estimate for each timestep, aligned on
        index. Works better when advantage estimates are normalized.
      current_policy_distribution: The policy distribution, evaluated on all
        time_steps.
      valid_mask: Mask for invalid timesteps. Float value 1.0 for valid
        timesteps and 0.0 for invalid timesteps. (Timesteps which either are
        betweeen two episodes, or part of an unfinished episode at the end of
        one batch dimension.)
      debug_summaries: True if debug summaries should be created.

    Returns:
      policy_gradient_loss: A tensor that will contain policy gradient loss for
        the on-policy experience.
    """
    nest.assert_same_structure(time_steps, self.time_step_spec())
    action_log_prob = common_utils.log_probability(
        current_policy_distribution, actions, self._action_spec)
    action_log_prob = tf.to_float(action_log_prob)
    if self._log_prob_clipping > 0.0:
      action_log_prob = tf.clip_by_value(action_log_prob,
                                         -self._log_prob_clipping,
                                         self._log_prob_clipping)
    if self._check_numerics:
      action_log_prob = tf.check_numerics(action_log_prob, 'action_log_prob')

    # Prepare both clipped and unclipped importance ratios.
    importance_ratio = tf.exp(action_log_prob - sample_action_log_probs)
    importance_ratio_clipped = tf.clip_by_value(
        importance_ratio,
        1 - self._importance_ratio_clipping,
        1 + self._importance_ratio_clipping)

    if self._check_numerics:
      importance_ratio = tf.check_numerics(importance_ratio, 'importance_ratio')
      if self._importance_ratio_clipping > 0.0:
        importance_ratio_clipped = tf.check_numerics(importance_ratio_clipped,
                                                     'importance_ratio_clipped')

    # Pessimistically choose the minimum objective value for clipped and
    #   unclipped importance ratios.
    per_timestep_objective = importance_ratio * advantages
    per_timestep_objective_clipped = importance_ratio_clipped * advantages
    per_timestep_objective_min = tf.minimum(per_timestep_objective,
                                            per_timestep_objective_clipped)

    if self._importance_ratio_clipping > 0.0:
      policy_gradient_loss = -per_timestep_objective_min
    else:
      policy_gradient_loss = -per_timestep_objective
    policy_gradient_loss = tf.reduce_mean(policy_gradient_loss * valid_mask)

    if debug_summaries:
      if self._importance_ratio_clipping > 0.0:
        clip_fraction = tf.reduce_mean(tf.to_float(
            tf.greater(tf.abs(importance_ratio - 1.0),
                       self._importance_ratio_clipping)))
        tf.contrib.summary.scalar('clip_fraction', clip_fraction)
      tf.contrib.summary.histogram('action_log_prob', action_log_prob)
      tf.contrib.summary.histogram('action_log_prob_sample',
                                   sample_action_log_probs)
      tf.contrib.summary.histogram('importance_ratio', importance_ratio)
      tf.contrib.summary.scalar('importance_ratio_mean',
                                tf.reduce_mean(importance_ratio))
      tf.contrib.summary.histogram('importance_ratio_clipped',
                                   importance_ratio_clipped)
      tf.contrib.summary.histogram('per_timestep_objective',
                                   per_timestep_objective)
      tf.contrib.summary.histogram('per_timestep_objective_clipped',
                                   per_timestep_objective_clipped)
      tf.contrib.summary.histogram('per_timestep_objective_min',
                                   per_timestep_objective_min)
      entropy = common_utils.entropy(current_policy_distribution,
                                     self.action_spec())
      tf.contrib.summary.histogram('policy_entropy', entropy)
      tf.contrib.summary.scalar('policy_entropy_mean', tf.reduce_mean(entropy))
      # Categorical distribution (used for discrete actions)
      # doesn't have a mean.
      if not self.action_spec().is_discrete():
        tf.contrib.summary.histogram('actions_distribution_mean',
                                     current_policy_distribution.mean())
        tf.contrib.summary.histogram('actions_distribution_stddev',
                                     current_policy_distribution.stddev())
      tf.contrib.summary.histogram('policy_gradient_loss', policy_gradient_loss)

    if self._check_numerics:
      policy_gradient_loss = tf.check_numerics(policy_gradient_loss,
                                               'policy_gradient_loss')

    return policy_gradient_loss

  def kl_cutoff_loss(self, kl_divergence, debug_summaries=False):
    # Squared penalization for mean KL divergence above some threshold.
    if self._kl_cutoff_factor <= 0.0:
      return tf.constant(0.0, dtype=tf.float32, name='zero_kl_cutoff_loss')
    kl_cutoff = self._kl_cutoff_factor * self._adaptive_kl_target
    mean_kl = tf.reduce_mean(kl_divergence)
    kl_over_cutoff = tf.maximum(mean_kl - kl_cutoff, 0.0)
    kl_cutoff_loss = self._kl_cutoff_coef * tf.square(kl_over_cutoff)

    if debug_summaries:
      tf.contrib.summary.scalar(
          'kl_cutoff_count',
          tf.reduce_sum(tf.to_int64(kl_divergence > kl_cutoff)))
      tf.contrib.summary.scalar('kl_cutoff_loss', kl_cutoff_loss)

    return tf.identity(kl_cutoff_loss, name='kl_cutoff_loss')

  def adaptive_kl_loss(self, kl_divergence, debug_summaries=False):
    if self._adaptive_kl_beta is None:
      return tf.constant(0.0, dtype=tf.float32, name='zero_adaptive_kl_loss')

    # Define the loss computation, which depends on the update computation.
    mean_kl = tf.reduce_mean(kl_divergence)
    adaptive_kl_loss = self._adaptive_kl_beta * mean_kl

    if debug_summaries:
      tf.contrib.summary.scalar('adaptive_kl_loss', adaptive_kl_loss)

    return adaptive_kl_loss

  def _kl_divergence(self,
                     time_steps,
                     action_distribution_parameters,
                     current_policy_distribution):
    outer_dims = list(range(nest_utils.get_outer_rank(
        time_steps, self.time_step_spec())))
    old_actions_distribution = (
        ppo_utils.get_distribution_from_params_and_classes(
            action_distribution_parameters,
            self._action_distribution_class_spec))
    kl_divergence = ppo_utils.nested_kl_divergence(
        old_actions_distribution, current_policy_distribution,
        outer_dims=outer_dims)
    return kl_divergence

  def kl_penalty_loss(self,
                      time_steps,
                      action_distribution_parameters,
                      current_policy_distribution,
                      valid_mask,
                      debug_summaries=False):
    """Compute a loss that penalizes policy steps with high KL.

    Based on KL divergence from old (data-collection) policy to new (updated)
    policy.

    All tensors should have a single batch dimension.

    Args:
      time_steps: TimeStep tuples with observations for each timestep. Used for
        computing new action distributions.
      action_distribution_parameters: Action distribution params of the data
        collection policy, used for reconstruction old action distributions.
      current_policy_distribution: The policy distribution, evaluated on all
        time_steps.
      valid_mask: Mask for invalid timesteps. Float value 1.0 for valid
        timesteps and 0.0 for invalid timesteps. (Timesteps which either are
        betweeen two episodes, or part of an unfinished episode at the end of
        one batch dimension.)
      debug_summaries: True if debug summaries should be created.
    Returns:
      kl_penalty_loss: The sum of a squared penalty for KL over a constant
        threshold, plus an adaptive penalty that encourages updates toward a
        target KL divergence.
    """
    kl_divergence = self._kl_divergence(
        time_steps, action_distribution_parameters,
        current_policy_distribution) * valid_mask

    if debug_summaries:
      tf.contrib.summary.histogram('kl_divergence', kl_divergence)

    kl_cutoff_loss = self.kl_cutoff_loss(kl_divergence, debug_summaries)
    adaptive_kl_loss = self.adaptive_kl_loss(kl_divergence, debug_summaries)
    return tf.add(kl_cutoff_loss, adaptive_kl_loss, name='kl_penalty_loss')

  def update_adaptive_kl_beta(self, kl_divergence):
    """Create update op for adaptive KL penalty coefficient.

    Args:
      kl_divergence: KL divergence of old policy to new policy for all
        timesteps.
    Returns:
      update_op: An op which runs the update for the adaptive kl penalty term.
    """
    if self._adaptive_kl_beta is None:
      return tf.no_op()

    mean_kl = tf.reduce_mean(kl_divergence)

    # Update the adaptive kl beta after each time it is computed.
    mean_kl_below_bound = (
        mean_kl < self._adaptive_kl_target * (1.0-self._adaptive_kl_tolerance))
    mean_kl_above_bound = (
        mean_kl > self._adaptive_kl_target * (1.0+self._adaptive_kl_tolerance))
    adaptive_kl_update_factor = tf.case(
        {mean_kl_below_bound: lambda: tf.constant(1.0/1.5, dtype=tf.float32),
         mean_kl_above_bound: lambda: tf.constant(1.5, dtype=tf.float32),},
        default=lambda: tf.constant(1.0, dtype=tf.float32), exclusive=True)
    new_adaptive_kl_beta = tf.maximum(
        self._adaptive_kl_beta * adaptive_kl_update_factor, 10e-16)
    update_adaptive_kl_beta = tf.assign(
        self._adaptive_kl_beta, new_adaptive_kl_beta)

    if self._debug_summaries:
      tf.contrib.summary.scalar('adaptive_kl_update_factor',
                                adaptive_kl_update_factor)
      tf.contrib.summary.scalar('mean_kl_divergence', mean_kl)
      tf.contrib.summary.scalar('adaptive_kl_beta', self._adaptive_kl_beta)

    return update_adaptive_kl_beta
