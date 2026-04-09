# -- coding: UTF-8
import os
import sys

sys.stdout = open(sys.stdout.fileno(), mode='w', buffering=1)
sys.stderr = open(sys.stderr.fileno(), mode='w', buffering=1)

from pathlib import Path

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
    os.chdir(str(ROOT))

import argparse
import threading

import h5py
import numpy as np
import rclpy
import yaml

from utils.ros_operator import RosOperator, Rate
from utils.setup_loader import setup_loader

np.set_printoptions(linewidth=200)
np.set_printoptions(suppress=True)

GRIPPER_INDEX = [6, 13]


def load_yaml(yaml_file):
    try:
        with open(yaml_file, 'r', encoding='utf-8') as file:
            return yaml.safe_load(file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Config file not found: {yaml_file}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Failed to parse YAML file: {yaml_file}") from exc


def resolve_path(path_like):
    path = Path(path_like).expanduser()
    if path.suffix != '.hdf5':
        path = path.with_suffix('.hdf5')

    if not path.is_absolute():
        path = ROOT / path

    return path.resolve()


def load_action_sequence(dataset_path, source):
    dataset_path = resolve_path(dataset_path)
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Dataset does not exist: {dataset_path}")

    dataset_key = '/action' if source == 'action' else '/observations/qpos'

    with h5py.File(dataset_path, 'r') as root:
        if dataset_key not in root:
            raise KeyError(f"Dataset key missing in HDF5: {dataset_key}")

        actions = root[dataset_key][()]

    actions = np.asarray(actions, dtype=np.float64)
    if actions.ndim != 2 or actions.shape[1] != 14:
        raise ValueError(f"Expected replay data shape (N, 14), got {actions.shape}")

    return dataset_path, actions


def apply_gripper_gate(action_value, gate):
    min_gripper = 0
    max_gripper = 5

    return min_gripper if action_value < gate else max_gripper


def split_action(action, gripper_gate):
    action = np.asarray(action, dtype=np.float64).copy()
    left_action = action[:GRIPPER_INDEX[0] + 1]
    right_action = action[GRIPPER_INDEX[0] + 1:GRIPPER_INDEX[1] + 1]

    if gripper_gate != -1:
        left_action[GRIPPER_INDEX[0]] = apply_gripper_gate(left_action[GRIPPER_INDEX[0]], gripper_gate)
        right_action[GRIPPER_INDEX[0]] = apply_gripper_gate(right_action[GRIPPER_INDEX[0]], gripper_gate)

    return left_action, right_action


def select_replay_window(actions, start_idx, end_idx):
    total_steps = len(actions)
    if start_idx < 0 or start_idx >= total_steps:
        raise ValueError(f"start_idx must be in [0, {total_steps - 1}], got {start_idx}")

    if end_idx == -1:
        end_idx = total_steps
    elif end_idx <= start_idx or end_idx > total_steps:
        raise ValueError(f"end_idx must be in ({start_idx}, {total_steps}], got {end_idx}")

    return actions[start_idx:end_idx], start_idx, end_idx


def spin_loop(node):
    while rclpy.ok():
        rclpy.spin_once(node, timeout_sec=0.001)


def move_to_first_action(ros_operator, first_action, gripper_gate):
    left_action, right_action = split_action(first_action, gripper_gate)
    print("Moving arms to the first replay frame...")
    ros_operator.follow_arm_publish_continuous(left_action, right_action)


def replay_actions(ros_operator, actions, frame_rate, gripper_gate, log_every, start_idx):
    rate = Rate(frame_rate)
    total_steps = len(actions)

    for offset, action in enumerate(actions):
        left_action, right_action = split_action(action, gripper_gate)
        ros_operator.follow_arm_publish(left_action, right_action)

        step_idx = start_idx + offset
        if offset == 0 or offset == total_steps - 1 or ((offset + 1) % log_every == 0):
            print(f"Replay step {offset + 1}/{total_steps} (dataset index {step_idx})")

        rate.sleep()


def parse_args(known=False):
    parser = argparse.ArgumentParser(description='Replay robot arm actions from an HDF5 episode.')

    parser.add_argument('--episode_path', type=str, required=True, help='path to episode .hdf5')
    parser.add_argument('--data', type=str, default=Path.joinpath(ROOT, 'data/config.yaml'),
                        help='ROS topic config yaml')
    parser.add_argument('--source', type=str, choices=['action', 'qpos'], default='action',
                        help='joint source in HDF5; action preserves gripper processing from collection')
    parser.add_argument('--frame_rate', type=int, default=60, help='publish rate in Hz')
    parser.add_argument('--start_idx', type=int, default=0, help='start frame index')
    parser.add_argument('--end_idx', type=int, default=-1, help='end frame index, exclusive; -1 means full episode')
    parser.add_argument('--log_every', type=int, default=60, help='log every N replay steps')
    parser.add_argument('--gripper_gate', type=float, default=-1, help='optional gripper gate, same as inference.py')
    parser.add_argument('--arm_feedback_timeout', type=float, default=10.0,
                        help='seconds to wait for arm feedback before failing')

    # Kept for RosOperator compatibility. This script only replays arm joints.
    parser.add_argument('--use_base', action='store_true', help='reserved; base replay is not implemented here')
    parser.add_argument('--record', choices=['Distance', 'Speed'], default='Distance',
                        help='reserved for RosOperator compatibility')
    parser.add_argument('--use_depth_image', action='store_true',
                        help='reserved for RosOperator compatibility')
    parser.add_argument('--is_compress', action='store_true',
                        help='reserved for RosOperator compatibility')

    return parser.parse_known_args()[0] if known else parser.parse_args()


def main(args):
    setup_loader(ROOT)

    if args.frame_rate <= 0:
        raise ValueError(f"frame_rate must be > 0, got {args.frame_rate}")
    if args.log_every <= 0:
        raise ValueError(f"log_every must be > 0, got {args.log_every}")

    config = load_yaml(args.data)
    dataset_path, actions = load_action_sequence(args.episode_path, args.source)
    actions, start_idx, end_idx = select_replay_window(actions, args.start_idx, args.end_idx)

    if args.use_base:
        print("Warning: this replay script currently replays arm joints only. Base commands are ignored.")

    print(f"Loaded replay data from: {dataset_path}")
    print(f"Replay source: {args.source}")
    print(f"Replay window: [{start_idx}, {end_idx}) -> {len(actions)} frames")

    rclpy.init()
    ros_operator = RosOperator(args, config, in_collect=False)
    spin_thread = threading.Thread(target=spin_loop, args=(ros_operator,), daemon=True)
    spin_thread.start()

    try:
        move_to_first_action(ros_operator, actions[0], args.gripper_gate)
        replay_actions(ros_operator, actions, args.frame_rate, args.gripper_gate, args.log_every, start_idx)
    except KeyboardInterrupt:
        print('Replay interrupted by user')
    finally:
        ros_operator.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)


if __name__ == '__main__':
    main(parse_args())
