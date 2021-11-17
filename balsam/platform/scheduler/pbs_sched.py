import logging
import os
import subprocess
import tempfile
import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import click
import dateutil.parser

from .scheduler import (
    SchedulerBackfillWindow,
    SchedulerJobLog,
    SchedulerJobStatus,
    SchedulerSubmitError,
    SubprocessSchedulerInterface,
)

PathLike = Union[Path, str]

logger = logging.getLogger(__name__)


def parse_cobalt_time_minutes(t_str: str) -> int:
    try:
        H, M, S = map(int, t_str.split(":"))
    except ValueError:
        return 0
    else:
        return H * 60 + M + round(S / 60)


class PBSScheduler(SubprocessSchedulerInterface):
    status_exe = "qstat"
    submit_exe = "qsub"
    delete_exe = "qdel"
    backfill_exe = "pbsnodes"

    # maps scheduler states to Balsam states
    _job_states = {
        "Q": "queued",
        "H": "queued",
        "T": "queued",
        "W": "queued",
        "S": "queued",
        "R": "running",
        "E": "running",
    }

    @staticmethod
    def _job_state_map(scheduler_state: str) -> str:
        return PBSScheduler._job_states.get(scheduler_state, "unknown")

    # maps Balsam status fields to the scheduler fields
    # should be a comprehensive list of scheduler status fields
    _status_fields = {
        "scheduler_id": "JobID",
        "state": "State",
        "wall_time_min": "WallTime",
        "queue": "Queue",
        "num_nodes": "Nodes",
        "project": "Project",
        "time_remaining_min": "TimeRemaining",
        "queued_time_min": "QueuedTime",
    }

    # when reading these fields from the scheduler apply
    # these maps to the string extracted from the output
    @staticmethod
    def _status_field_map(balsam_field: str) -> Optional[Callable[[str], Any]]:
        status_field_map: Dict[str, Callable[[str], Any]] = {
            "scheduler_id": lambda id: int(id),
            "state": PBSScheduler._job_state_map,
            "queue": lambda queue: str(queue),
            "num_nodes": lambda n: int(n),
            "time_remaining_min": parse_cobalt_time_minutes,
            "queued_time_min": parse_cobalt_time_minutes,
            "project": lambda project: str(project),
            "wall_time_min": parse_cobalt_time_minutes,
        }
        return status_field_map.get(balsam_field, None)

    # maps node list states to Balsam node states
    _node_states = {
        "busy": "busy",
        "idle": "idle",
        "cleanup-pending": "busy",
        "down": "busy",
        "allocated": "busy",
    }

    @staticmethod
    def _node_state_map(nodelist_state: str) -> str:
        try:
            return PBSScheduler._node_states[nodelist_state]
        except KeyError:
            logger.warning("node state %s is not recognized", nodelist_state)
            return "unknown"

    # maps the Balsam status fields to the node list fields
    # should be a comprehensive list of node list fields
    _nodelist_fields = {
        "id": "Node_id",
        "name": "Name",
        "queues": "Queues",
        "state": "Status",
        "mem": "MCDRAM",
        "numa": "NUMA",
        "wall_time_min": "Backfill",
    }

    # when reading these fields from the scheduler apply
    # these maps to the string extracted from the output
    @staticmethod
    def _nodelist_field_map(balsam_field: str) -> Callable[[str], Any]:
        nodelist_field_map = {
            "id": lambda id: int(id),
            "state": PBSScheduler._node_state_map,
            "queues": lambda x: x.split(":"),
            "wall_time_min": lambda x: parse_cobalt_time_minutes(x),
        }
        return nodelist_field_map.get(balsam_field, lambda x: x)

    @staticmethod
    def _render_submit_args(
        script_path: Union[Path, str], project: str, queue: str, num_nodes: int, wall_time_min: int, **kwargs: Any
    ) -> List[str]:
        args = [
            PBSScheduler.submit_exe,
            "-o",
            Path(script_path).with_suffix("").name,
            "-A",
            project,
            "-q",
            queue,
            "-l", f"select={num_nodes}",
            "-l", f"walltime=00:{ wall_time_min }:00",
            str(script_path),
        ]
        return args

    @staticmethod
    def _render_status_args(project: Optional[str], user: Optional[str], queue: Optional[str]) -> List[str]:
        args = [PBSScheduler.status_exe]
        args += "-f -F json".split()
        #if user is not None:
            #args += ["-u", user]
        if queue is not None:
            args += ["-q", queue]
        return args

    @staticmethod
    def _render_delete_args(job_id: Union[int, str]) -> List[str]:
        return [PBSScheduler.delete_exe, str(job_id)]

    @staticmethod
    def _render_backfill_args() -> List[str]:
        return [PBSScheduler.backfill_exe, "-a","-F","json"]

    @staticmethod
    def _parse_submit_output(submit_output: str) -> int:
        try:
            return int(submit_output.split('.')[0])
        except:
            # Catch errors here and handle
            raise

# implement to fill in status_fields from above
    @staticmethod
    def _parse_status_output(raw_output: str) -> Dict[int, SchedulerJobStatus]:
        # TODO: this can be much more efficient with a compiled regex findall()
        status_dict = {}
        j = json.loads(raw_output)
        date_format = '%a %b %d %H:%M:%S %Y'
        for jobidstr,job in j['Jobs'].items():
            status = {}
            jobid = jobidstr.split('.')[0]
            status['scheduler_id'] = jobid
            status['state'] = PBSScheduler._job_states[job['job_state']]
            W = job['Resource_List']['walltime'].split(':')
            status['wall_time_min'] = W[0]*60 + W[1]  # 00:00:00
            status['queue'] = job['queue']
            status['num_nodes'] = job['Resource_List']['nodect']
            status['project'] = job['project']
            status['time_remaining_min'] = (datetime.strptime(job['etime'], date_format) - datetime.now()).total_seconds()
            status['queued_time_min'] = (datetime.now() - datetime.strptime(job['qtime'], date_format)).total_seconds()
            status_dict[jobid] = SchedulerJobStatus(**status)
        return status_dict

    @staticmethod
    def _parse_backfill_output(stdout: str) -> Dict[str, List[SchedulerBackfillWindow]]:
        # fill in this method later to support backfill
        # turam: input here will be json via "pbsnodes -a -F json"
        return dict()
        # prior cobalt impl follows
        raw_lines = stdout.strip().split("\n")
        nodelist = []
        node_lines = raw_lines[2:]
        for line in raw_lines:
            try:
                line_dict = PBSScheduler._parse_nodelist_line(line)
            except (ValueError, TypeError):
                logger.debug(f"Cannot parse nodelist line: {line}")
            else:
                if line_dict["wall_time_min"] > 0 and line_dict["state"] == "idle":
                    nodelist.append(line_dict)

        windows = PBSScheduler._nodelist_to_backfill(nodelist)
        return windows

    @staticmethod
    def _parse_nodelist_line(line: str) -> Dict[str, Any]:
        fields = line.split()
        actual = len(fields)
        expected = len(PBSScheduler._nodelist_fields)

        if actual != expected:
            raise ValueError(f"Line has {actual} columns: expected {expected}:\n{fields}")

        status = {}
        for name, value in zip(PBSScheduler._nodelist_fields, fields):
            func = PBSScheduler._nodelist_field_map(name)
            status[name] = func(value)
        return status

    @staticmethod
    def _nodelist_to_backfill(
        nodelist: List[Dict[str, Any]],
    ) -> Dict[str, List[SchedulerBackfillWindow]]:
        queue_bf_times = defaultdict(list)
        windows = defaultdict(list)

        for entry in nodelist:
            bf_time = entry["wall_time_min"]
            queues = entry["queues"]
            for queue in queues:
                queue_bf_times[queue].append(bf_time)

        for queue, bf_times in queue_bf_times.items():
            # Mapping {bf_time: num_nodes}
            bf_counter = Counter(bf_times)
            # {
            #    queue_name: [(bf_time1, num_nodes1), (bf_time2, num_nodes2), ...],
            # }
            # For each queue, sorted with longer times first
            queue_bf_times[queue] = sorted(bf_counter.items(), reverse=True)

        for queue, bf_list in queue_bf_times.items():
            running_total = 0
            for bf_time, num_nodes in bf_list:
                running_total += num_nodes
                windows[queue].append(SchedulerBackfillWindow(num_nodes=running_total, wall_time_min=bf_time))
        return windows

    @staticmethod
    def _parse_time(line: str) -> datetime:
        time_str = line[: line.find("(UTC)")]
        return dateutil.parser.parse(time_str)

# implement
    @staticmethod
    def _parse_logs(scheduler_id: int, job_script_path: Optional[PathLike]) -> SchedulerJobLog:
        if job_script_path is None:
            logger.warning("No job script path provided; cannot parse logs from scheduler_id alone")
            return SchedulerJobLog()
        logfile = Path(job_script_path).with_suffix("e"+str(scheduler_id))
        try:
            logger.info(f"Attempting to parse {logfile}")
            cobalt_log = logfile.read_text()
        except FileNotFoundError:
            logger.warning(f"Could not parse log: no file {logfile}")
            return SchedulerJobLog()

        lines = [line.strip() for line in cobalt_log.split("\n") if "(UTC)" in line]
        start_time = None
        for line in lines:
            if "COBALT_STARTTIME" in line:
                start_time = PBSScheduler._parse_time(line)
                break
        if start_time:
            end_time = PBSScheduler._parse_time(lines[-1])
            return SchedulerJobLog(start_time=start_time, end_time=end_time)
        logger.warning(f"Could not parse log: no line containing COBALT_STARTTIME in {logfile}")
        return SchedulerJobLog()

    @classmethod
    def discover_projects(cls) -> List[str]:
        """
        Get the user's allowed/preferred allocations
        Note: Could use sbank; currently uses Cobalt reporting of valid
              projects when an invalid project is given
        """
        click.echo("Checking with PBS for your current allocations...")
        with tempfile.NamedTemporaryFile() as fp:
            os.chmod(fp.name, 0o777)
            proc = subprocess.run(
                f"qsub -t 35 -n 1 -A placeholder_null {fp.name}",
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
            )

        projects = []
        for line in proc.stdout.split("\n"):
            if "Projects available" in line:
                projects = line.split()[2:]
                break

        if not projects:
            projects = super().discover_projects()
        return projects
