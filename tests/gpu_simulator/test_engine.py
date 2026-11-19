"""Unit tests for the GPU fleet simulator's topology, physics, faults and logs."""

import random
import unittest

from gpusim import logs as logmod
from gpusim.engine import FAULT_KINDS, MAX_GPUS, PRESETS, Fleet
from gpusim.exposition import DCGM_METRICS, render_dcgm
from gpusim.topology import REGION_SPECS, build_nodes

T0 = 1_700_000_000.0
LABELS_SHARED_BY_METRICS_AND_LOGS = ("region", "availability_zone", "datacenter", "cluster", "rack", "node")


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def make_fleet(seed=7, nodes=2, gpus=4, log_rate=0.2):
    clock = Clock()
    logs = []
    fleet = Fleet(nodes, gpus, rng=random.Random(seed), clock=clock, log_sink=logs.append)
    fleet.settings.log_rate = log_rate
    return fleet, clock, logs


def run(fleet, clock, ticks, dt=5.0):
    for _ in range(ticks):
        clock.now += dt
        fleet.tick(clock.now, dt)


def gpus_of(fleet, **match):
    return [g for n in fleet.nodes for g in n.gpus if all(getattr(g, k) == v for k, v in match.items())]


class TopologyTest(unittest.TestCase):
    def test_fleet_shape(self):
        nodes = build_nodes(4, 8)
        gpus = [g for n in nodes for g in n.gpus]
        self.assertEqual(len(REGION_SPECS), 6)
        self.assertEqual(len(nodes), 6 * 2 * 4)
        self.assertEqual(len(gpus), 6 * 2 * 4 * 8)
        self.assertEqual(len({g.uuid for g in gpus}), len(gpus), "UUIDs must be unique")
        self.assertEqual(len({g.key for g in gpus}), len(gpus))

    def test_deterministic(self):
        a = [(n.name, n.workload, n.pod) for n in build_nodes(3, 2)]
        b = [(n.name, n.workload, n.pod) for n in build_nodes(3, 2)]
        self.assertEqual(a, b)

    def test_regions_use_their_own_gpu_model_and_two_azs(self):
        by_region = {}
        for n in build_nodes(2, 1):
            by_region.setdefault(n.region, []).append(n)
        for spec in REGION_SPECS:
            nodes = by_region[spec.name]
            self.assertEqual({n.gpus[0].model.key for n in nodes}, {spec.gpu_model})
            self.assertEqual({n.datacenter for n in nodes}, set(spec.datacenters))
            self.assertEqual(len({n.availability_zone for n in nodes}), 2)

    def test_gpu_labels(self):
        gpu = build_nodes(1, 2)[0].gpus[1]
        for key in ("gpu", "UUID", "modelName", "Hostname", "pci_bus_id", "instance", *LABELS_SHARED_BY_METRICS_AND_LOGS):
            self.assertIn(key, gpu.labels)
        self.assertEqual(gpu.labels["gpu"], "1")
        self.assertEqual(gpu.labels["node"], gpu.labels["Hostname"])


class PhysicsTest(unittest.TestCase):
    def setUp(self):
        self.fleet, self.clock, self.logs = make_fleet()

    def test_values_stay_in_physical_bounds(self):
        run(self.fleet, self.clock, 40)
        for g in gpus_of(self.fleet):
            self.assertTrue(0 <= g.util <= 100)
            self.assertTrue(0 <= g.fb_used <= g.fb_total)
            self.assertAlmostEqual(g.fb_used + g.fb_free, g.fb_total)
            self.assertTrue(g.model.idle_w * 0.9 <= g.power <= g.model.tdp_w * 1.1)
            self.assertLess(g.gpu_temp, g.slowdown_temp + 15)

    def test_counters_never_decrease(self):
        counters = [a for _, t, _, a, _ in DCGM_METRICS if t == "counter"]
        self.fleet.add_fault("ecc", {}, 0, 0.9)
        self.fleet.add_fault("nvlink", {}, 0, 0.9)
        run(self.fleet, self.clock, 3)
        before = {(g.key, a): getattr(g, a) for g in gpus_of(self.fleet) for a in counters}
        run(self.fleet, self.clock, 20)
        for g in gpus_of(self.fleet):
            for a in counters:
                self.assertGreaterEqual(getattr(g, a), before[(g.key, a)], f"{a} went backwards")

    def test_forced_load_drives_utilization_power_and_temperature(self):
        self.fleet.settings.intensity = 0.0
        run(self.fleet, self.clock, 40)
        gpu = gpus_of(self.fleet)[0]
        base_power, base_temp = gpu.power, gpu.gpu_temp
        self.assertLess(gpu.util, 5)
        self.fleet.add_fault("load", {}, 0, 1.0, {"value": 100})
        run(self.fleet, self.clock, 40)
        self.assertGreater(gpu.util, 90)
        self.assertGreater(gpu.power, base_power + 100)
        self.assertGreater(gpu.gpu_temp, base_temp + 10)

    def test_thermal_fault_throttles_and_logs(self):
        self.fleet.add_fault("load", {}, 0, 1.0, {"value": 100})
        self.fleet.add_fault("thermal", {"region": "eu-west-1"}, 0, 1.0)
        run(self.fleet, self.clock, 120)
        hot = gpus_of(self.fleet, region="eu-west-1")
        cool = gpus_of(self.fleet, region="us-east-1")
        self.assertGreater(min(g.gpu_temp for g in hot), max(g.gpu_temp for g in cool) + 10)
        self.assertTrue(all(g.thermal_violation > 0 for g in hot))
        self.assertTrue(all(g.throttle_reasons & 0x60 for g in hot))
        self.assertTrue(all(g.sm_clock < g.model.max_sm_clock * 0.9 for g in hot))
        self.assertTrue(all(g.thermal_violation == 0 for g in cool))
        messages = [r["_msg"] for r in self.logs if r["region"] == "eu-west-1" and r["app"] == "dcgm-exporter"]
        self.assertTrue(any("exceeds slowdown threshold" in m for m in messages))

    def test_power_cap_limits_power_and_counts_violations(self):
        self.fleet.add_fault("load", {}, 0, 1.0, {"value": 100})
        self.fleet.add_fault("power_cap", {"region": "us-west-2"}, 0, 1.0)
        run(self.fleet, self.clock, 40)
        for g in gpus_of(self.fleet, region="us-west-2"):
            self.assertLessEqual(g.power, g.power_limit * 1.1)
            self.assertLess(g.power_limit, g.model.tdp_w * 0.6)
            self.assertGreater(g.power_violation, 0)
            self.assertTrue(g.throttle_reasons & 0x4)

    def test_memory_leak_fills_framebuffer_and_logs_oom(self):
        self.fleet.add_fault("mem_leak", {"region": "us-east-1"}, 0, 1.0)
        run(self.fleet, self.clock, 130)  # the leak ramps over 10 minutes
        busy = [g for g in gpus_of(self.fleet, region="us-east-1") if g.workload != "idle"]
        self.assertTrue(busy)
        self.assertTrue(all(g.fb_used / g.fb_total > 0.97 for g in busy))
        self.assertTrue(any("out of memory" in r["_msg"] for r in self.logs))

    def test_idle_gpu_reports_idle_throttle_reason(self):
        self.fleet.settings.intensity = 0.0
        run(self.fleet, self.clock, 30)
        self.assertTrue(all(g.throttle_reasons & 0x1 for g in gpus_of(self.fleet)))


class FaultTest(unittest.TestCase):
    def setUp(self):
        self.fleet, self.clock, self.logs = make_fleet(nodes=2, gpus=4)

    def test_xid_fault_logs_and_sets_gauge(self):
        node = self.fleet.nodes[0]
        self.fleet.add_fault("xid", {"node": node.name}, 0, 1.0, {"xid": 79})
        run(self.fleet, self.clock, 40)
        records = [r for r in self.logs if r.get("xid") == "79" and r["app"] == "kernel"]
        self.assertTrue(records)
        self.assertTrue(all(r["node"] == node.name for r in records))
        self.assertTrue(all("Xid" in r["_msg"] and "fallen off the bus" in r["_msg"] for r in records))
        self.assertTrue(any(g.xid == 79 for g in node.gpus))
        self.assertTrue(all(g.xid == 0 for g in self.fleet.nodes[1].gpus))

    def test_xid_gauge_clears_after_fault_ends(self):
        node = self.fleet.nodes[0]
        self.fleet.add_fault("xid", {"node": node.name}, 20, 1.0, {"xid": 31})
        run(self.fleet, self.clock, 8)
        self.assertTrue(any(g.xid == 31 for g in node.gpus))
        run(self.fleet, self.clock, 80)  # past the 5 minute hold
        self.assertTrue(all(g.xid == 0 for g in node.gpus))
        self.assertEqual(self.fleet.summary()["faults"], [])

    def test_ecc_fault_increments_counters_and_remaps_rows_at_high_intensity(self):
        self.fleet.add_fault("ecc", {"region": "us-east-1"}, 0, 1.0)
        run(self.fleet, self.clock, 400)
        gpus = gpus_of(self.fleet, region="us-east-1")
        self.assertTrue(all(g.ecc_sbe_vol > 0 and g.ecc_sbe_agg > 0 for g in gpus))
        self.assertTrue(any(g.ecc_dbe_vol > 0 and g.remap_uncorr > 0 for g in gpus))
        self.assertTrue(all(g.ecc_sbe_vol == 0 for g in gpus_of(self.fleet, region="us-west-2")))

    def test_nvlink_fault_only_affects_nvlink_boards(self):
        self.fleet.add_fault("nvlink", {}, 0, 1.0)
        run(self.fleet, self.clock, 40)
        for g in gpus_of(self.fleet):
            if g.model.nvlink_gbs:
                self.assertGreater(g.nvlink_crc_flit, 0)
            else:
                self.assertEqual(g.nvlink_crc_flit, 0)

    def test_pcie_fault_degrades_link_and_counts_replays(self):
        self.fleet.add_fault("pcie", {"region": "us-east-1"}, 0, 1.0)
        run(self.fleet, self.clock, 5)
        for g in gpus_of(self.fleet, region="us-east-1"):
            self.assertEqual(g.link_width, 4)
            self.assertEqual(g.link_gen, g.model.pcie_gen - 1)
            self.assertGreater(g.pcie_replay, 0)

    def test_gpu_lost_hides_only_that_gpu_and_recovers(self):
        node = self.fleet.nodes[0]
        fault = self.fleet.add_fault("gpu_lost", {"node": node.name, "gpu": 2})
        run(self.fleet, self.clock, 2)
        text = render_dcgm(self.fleet)
        self.assertNotIn(node.gpus[2].uuid, text)
        self.assertIn(node.gpus[1].uuid, text)
        lost = [r for r in self.logs if r.get("xid") == "79"]
        self.assertTrue(lost and all(r["gpu"] == "2" for r in lost if r["app"] == "kernel"))
        self.assertTrue(any(r["app"] == "nvidia-device-plugin" and r["level"] == "error" for r in lost))
        self.fleet.remove_fault(fault.id)
        run(self.fleet, self.clock, 2)
        self.assertIn(node.gpus[2].uuid, render_dcgm(self.fleet))
        self.assertTrue(any("healthy again" in r["_msg"] for r in self.logs))

    def test_node_down_silences_metrics_and_logs(self):
        node = self.fleet.nodes[0]
        self.fleet.settings.log_rate = 5
        self.fleet.add_fault("node_down", {"node": node.name})
        run(self.fleet, self.clock, 10)
        self.assertNotIn(f'node="{node.name}"', render_dcgm(self.fleet))
        self.assertFalse([r for r in self.logs if r["node"] == node.name])
        self.assertTrue([r for r in self.logs if r["node"] != node.name])

    def test_region_disabled_silences_region(self):
        self.fleet.regions["ap-south-1"].enabled = False
        self.fleet.settings.log_rate = 5
        run(self.fleet, self.clock, 10)
        self.assertNotIn('region="ap-south-1"', render_dcgm(self.fleet))
        self.assertFalse([r for r in self.logs if r["region"] == "ap-south-1"])
        summary = {r["name"]: r for r in self.fleet.summary()["regions"]}
        self.assertEqual(summary["ap-south-1"]["gpus_up"], 0)

    def test_faults_expire(self):
        self.fleet.add_fault("thermal", {}, 30, 0.5)
        run(self.fleet, self.clock, 3)
        self.assertEqual(len(self.fleet.faults), 1)
        run(self.fleet, self.clock, 4)
        self.assertEqual(len(self.fleet.faults), 0)

    def test_validation(self):
        bad = [
            dict(kind="nope"),
            dict(kind="thermal", scope={"region": "mars-1"}),
            dict(kind="thermal", scope={"rack": "rack-01"}),
            dict(kind="thermal", scope={"gpu": 1}),
            dict(kind="thermal", scope={"node": self.fleet.nodes[0].name, "gpu": 99}),
            dict(kind="thermal", intensity=2),
            dict(kind="thermal", duration_s=-1),
            dict(kind="xid", params={"xid": 12345}),
            dict(kind="load", params={"value": 500}),
        ]
        for kwargs in bad:
            with self.subTest(kwargs), self.assertRaises(ValueError):
                self.fleet.add_fault(**kwargs)
        self.assertEqual(len(self.fleet.faults), 0)

    def test_every_documented_fault_kind_can_be_injected(self):
        for kind in FAULT_KINDS:
            self.fleet.add_fault(kind, {"region": "us-east-1"}, 60, 0.5)
        run(self.fleet, self.clock, 5)
        self.assertEqual(len(self.fleet.faults), len(FAULT_KINDS))

    def test_every_preset_applies(self):
        for name in PRESETS:
            with self.subTest(name):
                self.fleet.reset()
                self.fleet.apply_preset(name)
                run(self.fleet, self.clock, 3)
        with self.assertRaises(ValueError):
            self.fleet.apply_preset("nope")

    def test_reset_clears_everything(self):
        self.fleet.add_fault("thermal", {}, 0, 0.5)
        self.fleet.regions["us-east-1"].enabled = False
        self.fleet.settings.intensity = 0.2
        self.fleet.reset()
        self.assertEqual(self.fleet.faults, {})
        self.assertTrue(self.fleet.regions["us-east-1"].enabled)
        self.assertEqual(self.fleet.settings.intensity, 1.0)


class LogTest(unittest.TestCase):
    def test_records_carry_the_labels_metrics_use(self):
        fleet, clock, logs = make_fleet(log_rate=3)
        run(fleet, clock, 10)
        self.assertGreater(len(logs), 50)
        gpu_keys = {g.uuid: g for g in gpus_of(fleet)}
        for rec in logs:
            for key in ("_time", "_msg", "level", "app", *LABELS_SHARED_BY_METRICS_AND_LOGS):
                self.assertIn(key, rec)
            self.assertIn(rec["level"], logmod.LEVELS)
            self.assertTrue(rec["_time"].endswith("Z"))
            gpu = gpu_keys[rec["UUID"]]
            self.assertEqual(rec["node"], gpu.node)
            self.assertEqual(rec["region"], gpu.region)
            self.assertEqual(rec["gpu"], str(gpu.index))
            self.assertEqual(rec["modelName"], gpu.model.model_name)

    def test_level_ratios_are_respected(self):
        fleet, clock, logs = make_fleet(nodes=4, gpus=2, log_rate=10)
        fleet.settings.update({"warn_ratio": 0.0, "error_ratio": 0.0})
        run(fleet, clock, 6)
        self.assertEqual({r["level"] for r in logs}, {"info"})
        fleet.settings.update({"warn_ratio": 0.0, "error_ratio": 1.0})
        logs.clear()
        run(fleet, clock, 6)
        self.assertEqual({r["level"] for r in logs}, {"error"})

    def test_log_rate_zero_and_disabled_emit_nothing(self):
        fleet, clock, logs = make_fleet(log_rate=0)
        run(fleet, clock, 10)
        self.assertEqual(logs, [])
        fleet.settings.update({"log_rate": 5, "logs_enabled": False})
        run(fleet, clock, 10)
        self.assertEqual(logs, [])

    def test_sources_can_be_switched_off(self):
        fleet, clock, logs = make_fleet(log_rate=5)
        fleet.settings.update({"sources": {"workload": False, "kernel": False}})
        run(fleet, clock, 20)
        apps = {r["app"] for r in logs}
        self.assertTrue(apps)
        self.assertFalse(apps & {"training-job", "inference-server", "batch-job", "kernel"})

    def test_fault_logs_respect_source_toggle(self):
        fleet, clock, logs = make_fleet(log_rate=0)
        fleet.settings.update({"sources": {"kernel": False}})
        fleet.add_fault("xid", {}, 0, 1.0)
        run(fleet, clock, 20)
        self.assertFalse([r for r in logs if r["app"] == "kernel"])

    def test_burst(self):
        fleet, clock, logs = make_fleet(log_rate=0)
        node = fleet.nodes[3]
        self.assertEqual(fleet.burst(25, "error", "kernel", {"node": node.name}, "boom"), 25)
        self.assertEqual(len(logs), 25)
        self.assertTrue(all(r["_msg"] == "boom" and r["node"] == node.name and r["level"] == "error" for r in logs))
        self.assertEqual(len(fleet.recent_logs(100, node.region, "error")), 25)
        self.assertEqual(fleet.recent_logs(100, "no-such-region"), [])
        for kwargs in (dict(count=0), dict(count=10**6), dict(count=1, level="fatal"),
                       dict(count=1, app="nope"), dict(count=1, scope={"node": "nope"})):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                fleet.burst(**kwargs)

    def test_paused_simulation_does_not_advance(self):
        fleet, clock, logs = make_fleet(log_rate=5)
        fleet.settings.update({"paused": True})
        before = render_dcgm(fleet)
        run(fleet, clock, 10)
        self.assertEqual(render_dcgm(fleet), before)
        self.assertEqual(logs, [])


class SettingsTest(unittest.TestCase):
    def test_invalid_updates_change_nothing(self):
        fleet, _, _ = make_fleet()
        for body in ({"intensity": 5}, {"intensity": "high"}, {"paused": "yes"}, {"nope": 1},
                     {"sources": {"nope": True}}, {"warn_ratio": 0.7, "error_ratio": 0.7}, [1]):
            with self.subTest(body), self.assertRaises(ValueError):
                fleet.settings.update(body)
        self.assertEqual(fleet.settings.as_dict(), make_fleet()[0].settings.as_dict())

    def test_region_update(self):
        fleet, _, _ = make_fleet()
        fleet.regions["us-east-1"].update({"load": 1.5, "ambient_offset": 5, "enabled": False})
        self.assertEqual(fleet.regions["us-east-1"].load, 1.5)
        with self.assertRaises(ValueError):
            fleet.regions["us-east-1"].update({"load": 9})

    def test_region_load_and_ambient_change_the_model(self):
        fleet, clock, _ = make_fleet()
        fleet.add_fault("load", {}, 0, 1.0, {"value": 80})
        fleet.regions["eu-west-1"].update({"ambient_offset": 25})
        run(fleet, clock, 80)
        hot = gpus_of(fleet, region="eu-west-1")
        base = gpus_of(fleet, region="eu-central-1")
        self.assertGreater(min(g.gpu_temp for g in hot), max(g.gpu_temp for g in base))


class ResizeTest(unittest.TestCase):
    def test_resize_keeps_existing_state(self):
        fleet, clock, _ = make_fleet(nodes=2, gpus=2)
        run(fleet, clock, 5)
        target = fleet.nodes[0].gpus[0]
        energy, key = target.energy, target.key
        fleet.resize(4, 2)
        self.assertEqual(sum(len(n.gpus) for n in fleet.nodes), 6 * 2 * 4 * 2)
        kept = next(g for n in fleet.nodes for g in n.gpus if g.key == key)
        self.assertEqual(kept.energy, energy)

    def test_resize_drops_faults_on_removed_nodes(self):
        fleet, _, _ = make_fleet(nodes=4, gpus=2)
        removed = [n.name for n in fleet.nodes if n.name.endswith("n04")][0]
        fleet.add_fault("xid", {"node": removed})
        fleet.resize(2, 2)
        self.assertEqual(fleet.faults, {})

    def test_limits(self):
        fleet, _, _ = make_fleet()
        for args in ((0, 8), (65, 8), (4, 0), (4, 17), (True, 8), ("4", 8), (64, 16)):
            with self.subTest(args), self.assertRaises(ValueError):
                fleet.resize(*args)
        self.assertLessEqual(len(REGION_SPECS) * 2 * 64 * 4, MAX_GPUS)


class ExpositionTest(unittest.TestCase):
    def setUp(self):
        self.fleet, self.clock, _ = make_fleet(nodes=2, gpus=2)
        run(self.fleet, self.clock, 3)
        self.text = render_dcgm(self.fleet)

    def test_format(self):
        seen_type = set()
        for line in self.text.splitlines():
            if line.startswith("# TYPE "):
                _, _, name, mtype = line.split()
                self.assertIn(mtype, ("gauge", "counter"))
                seen_type.add(name)
            elif line.startswith("# HELP "):
                continue
            else:
                name, _, rest = line.partition("{")
                self.assertIn(name, seen_type, f"sample before TYPE: {line}")
                labels, _, value = rest.rpartition("} ")
                float(value)
                self.assertTrue(labels.startswith('gpu="'))

    def test_one_series_per_gpu_per_metric(self):
        gpus = gpus_of(self.fleet)
        nvlink = [g for g in gpus if g.model.nvlink_gbs]
        for name, _, _, _, nvlink_only in DCGM_METRICS:
            samples = [line for line in self.text.splitlines() if line.startswith(name + "{")]
            self.assertEqual(len(samples), len(nvlink if nvlink_only else gpus), name)

    def test_nvidia_dcgm_names_present(self):
        for name in ("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_POWER_USAGE", "DCGM_FI_DEV_FB_USED",
                     "DCGM_FI_DEV_XID_ERRORS", "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL"):
            self.assertIn(f"# TYPE {name} ", self.text)

    def test_region_filter(self):
        text = render_dcgm(self.fleet, "eu-west-1")
        self.assertIn('region="eu-west-1"', text)
        self.assertNotIn('region="us-east-1"', text)

    def test_labels_are_escaped(self):
        gpu = gpus_of(self.fleet)[0]
        from gpusim.topology import _escape

        self.assertEqual(_escape('a"b\\c\nd'), 'a\\"b\\\\c\\nd')
        self.assertIn(f'UUID="{gpu.uuid}"', self.text)


if __name__ == "__main__":
    unittest.main()
