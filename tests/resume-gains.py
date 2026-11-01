#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute the production gain/lease loop against a clamping mixer fixture."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "src/main.rs").read_text()
start = source.index("            let mut all_nominal = true;")
end = source.index("\n        }\n    });", start)
body = source[start:end]
assert "gain_restore_pending = true;" in source[source.index("if e.errno() == libc::ESTRPIPE"):start]

harness = r'''
use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
macro_rules! info { ($($arg:tt)*) => {{}} }

const UNLOCK_MAGIC: i32 = 42;
struct Ctl {
    unlocked: Cell<bool>, quiesced: Cell<bool>, fail_ping: Cell<bool>,
    values: RefCell<Vec<f32>>, writes: Cell<usize>, models: Cell<usize>,
}
struct Elem;
impl Elem {
    fn write_int(&mut self, ctl: &Ctl, value: i32) {
        assert_eq!(value, UNLOCK_MAGIC);
        assert!(!ctl.fail_ping.get(), "lease renewal failed");
        if !ctl.quiesced.get() { ctl.unlocked.set(true); }
    }
    fn read_int(&mut self, ctl: &Ctl) -> i32 { ctl.unlocked.get() as i32 }
}
struct Speaker { index: usize, model_gain: f32 }
impl Speaker {
    fn run_model(&mut self, _samples: &[i16], _rate: f32) -> f32 { self.model_gain }
    fn update(&mut self, ctl: &Ctl, gain: f32) {
        ctl.values.borrow_mut()[self.index] = if ctl.unlocked.get() { gain } else { -100.0 };
        ctl.writes.set(ctl.writes.get() + 1);
    }
}
struct Group { speakers: Vec<Speaker>, gain: f32 }
struct Args { max_reduction: Option<f32> }

fn tick(ctl: &Ctl, groups: &mut BTreeMap<usize, Group>, pending: &mut bool) {
    let mut gain_restore_pending = *pending;
    let mut unlock_elem = Elem;
    let mut once_nominal = false;
    let sample_rate = 48000;
    let buf_read = &[0i16; 24][..];
    let args = Args { max_reduction: None };
    ctl.models.set(ctl.models.get() + 1);
    BODY
    *pending = gain_restore_pending;
}

fn fixture(gain: f32) -> (Ctl, BTreeMap<usize, Group>) {
    let ctl = Ctl { unlocked: Cell::new(true), quiesced: Cell::new(false),
        fail_ping: Cell::new(false), values: RefCell::new(vec![gain; 6]),
        writes: Cell::new(0), models: Cell::new(0) };
    let groups = (0..2).map(|g| (g, Group { gain,
        speakers: (g*3..g*3+3).map(|index| Speaker { index, model_gain: gain }).collect()
    })).collect();
    (ctl, groups)
}
fn suspend(ctl: &Ctl) {
    ctl.unlocked.set(false);
    ctl.values.borrow_mut().fill(-100.0);
}

#[test]
fn normal_unchanged_gain_does_not_rewrite_controls() {
    let (ctl, mut groups) = fixture(0.0);
    let mut pending = false;
    tick(&ctl, &mut groups, &mut pending);
    tick(&ctl, &mut groups, &mut pending);
    assert_eq!(ctl.writes.get(), 0);
}
#[test]
fn resume_restores_all_six_unchanged_gains_after_unlock() {
    let (ctl, mut groups) = fixture(0.0);
    suspend(&ctl);
    let mut pending = true;
    tick(&ctl, &mut groups, &mut pending);
    assert_eq!(*ctl.values.borrow(), vec![-100.0; 6]);
    tick(&ctl, &mut groups, &mut pending);
    assert_eq!(*ctl.values.borrow(), vec![0.0; 6]);
    assert_eq!(ctl.writes.get(), 6);
    tick(&ctl, &mut groups, &mut pending);
    assert_eq!(ctl.writes.get(), 6);
}
#[test]
fn resume_keeps_model_reduction_and_uses_the_next_sample_result() {
    let (ctl, mut groups) = fixture(-12.0);
    suspend(&ctl);
    let mut pending = true;
    tick(&ctl, &mut groups, &mut pending);
    for group in groups.values_mut() {
        for speaker in &mut group.speakers { speaker.model_gain = -16.0; }
    }
    tick(&ctl, &mut groups, &mut pending);
    assert_eq!(*ctl.values.borrow(), vec![-16.0; 6]);
}
#[test]
fn successful_ping_during_quiesce_does_not_finish_restoration() {
    let (ctl, mut groups) = fixture(-6.0);
    suspend(&ctl);
    ctl.quiesced.set(true);
    let mut pending = true;
    for _ in 0..3 {
        tick(&ctl, &mut groups, &mut pending);
        assert!(pending);
        assert_eq!(*ctl.values.borrow(), vec![-100.0; 6]);
    }
    ctl.quiesced.set(false);
    tick(&ctl, &mut groups, &mut pending);
    assert!(!pending);
    assert_eq!(*ctl.values.borrow(), vec![-100.0; 6]);
    tick(&ctl, &mut groups, &mut pending);
    assert_eq!(*ctl.values.borrow(), vec![-6.0; 6]);
}
#[test]
fn failed_lease_renewal_does_not_restore_nominal_gain() {
    let (ctl, mut groups) = fixture(0.0);
    suspend(&ctl);
    ctl.fail_ping.set(true);
    let mut pending = true;
    assert!(catch_unwind(AssertUnwindSafe(|| tick(&ctl, &mut groups, &mut pending))).is_err());
    assert_eq!(*ctl.values.borrow(), vec![-100.0; 6]);
    assert_eq!(ctl.writes.get(), 0);
}
'''

def run_body(loop, directory, label):
    rust = directory / (label + ".rs")
    binary = directory / label
    rust.write_text(harness.replace("    BODY", loop))
    subprocess.run(["rustc", "--edition=2021", "--test", "-Awarnings",
                    str(rust), "-o", str(binary)], check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True)

with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    # Remove only the candidate's final invalidation block to reproduce the
    # original loop, which keeps the cached gain after the kernel clamps it.
    old = body[:body.index("\n            if gain_restore_pending {")]
    old_result = run_body(old, directory, "before")
    assert old_result.returncode != 0, "Original loop unexpectedly restored gains"
    assert "resume_restores_all_six_unchanged_gains_after_unlock ... FAILED" in old_result.stdout
    print("Original unchanged-gain resume failure: reproduced")
    result = run_body(body, directory, "after")
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr)
    assert result.returncode == 0
