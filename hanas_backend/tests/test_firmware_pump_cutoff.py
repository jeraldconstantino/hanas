"""Run the actual firmware countdown with a slow-network ESP timer harness."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_pump_deadline_survives_blocked_http_and_late_start(tmp_path):
    compiler = shutil.which('g++')
    if not compiler:
        pytest.skip('Native C++ compiler is unavailable')
    source = (Path(__file__).resolve().parents[2] / 'embedded_system/hanas_sensor_controller/hanas_sensor_controller.ino').read_text()
    start = source.index('bool runDosingCountdown(int pin, unsigned long durationMs, const String& pumpName) {')
    end = source.index('\nbool runInterDoseMixingCountdown(', start)
    harness = r'''
#include <atomic>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <cassert>
using String = std::string;
using Clock = std::chrono::steady_clock;
const auto origin = Clock::now();
unsigned long millis() { return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now()-origin).count(); }
void delay(unsigned long ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }
struct SerialStub { template<class T> void print(T) {} template<class T> void print(T,int) {} template<class T> void println(T) {} } Serial;
using portMUX_TYPE = std::mutex;
portMUX_TYPE dosingCutoffMux;
volatile bool dosingCutoffExpired = false;
#define portENTER_CRITICAL(mux) (mux)->lock()
#define portEXIT_CRITICAL(mux) (mux)->unlock()
constexpr int LOW=0, HIGH=1, MODE_FULL_CONTROL=1, MODE_MONITORING=0;
int controllerMode=MODE_FULL_CONTROL;
bool emergencyStopLatched=false;
constexpr unsigned long EMERGENCY_STOP_POLL_MS=1;
std::atomic<bool> pumpRunning{false};
std::atomic<unsigned long> switchedOn{0}, switchedOff{0};
void digitalWrite(int,int state) {
  if(state==HIGH) { switchedOn=millis(); pumpRunning=true; }
  else if(pumpRunning.exchange(false)) { switchedOff=millis(); }
}
void pumpOn(int pin) { digitalWrite(pin,HIGH); }
void pumpOff(int pin) { digitalWrite(pin,LOW); }
void stopAllPumps() { pumpOff(1); }
void enterMonitoringMode() { controllerMode=MODE_MONITORING; stopAllPumps(); }
void processSerialCommands() {}
bool backendEmergencyStopActive() { delay(200); return false; }
constexpr int ESP_OK=0, ESP_TIMER_TASK=0;
struct esp_timer_create_args_t { void(*callback)(void*)=nullptr; void* arg=nullptr; int dispatch_method=0; const char* name=nullptr; };
struct Timer { esp_timer_create_args_t args; std::atomic<bool> active{true}; std::thread thread; };
using esp_timer_handle_t=Timer*;
bool failCreate=false, delayStart=false;
int esp_timer_create(esp_timer_create_args_t* args,esp_timer_handle_t* handle) {
  if(failCreate) return -1;
  *handle=new Timer; (*handle)->args=*args; return ESP_OK;
}
int esp_timer_start_once(Timer* timer,uint64_t us) {
  timer->thread=std::thread([timer,us]{std::this_thread::sleep_for(std::chrono::microseconds(us));if(timer->active)timer->args.callback(timer->args.arg);});
  if(delayStart) delay(80);
  return ESP_OK;
}
int esp_timer_stop(Timer* timer) { timer->active=false; return ESP_OK; }
int esp_timer_delete(Timer* timer) { if(timer->thread.joinable())timer->thread.join();delete timer;return ESP_OK; }
'''
    harness += source[start:end]
    harness += r'''
int main() {
  // The HTTP call blocks for 200 ms; a 100 ms pulse must end independently.
  assert(runDosingCountdown(1,100,"ph_down"));
  assert(!pumpRunning);
  assert(switchedOff.load()-switchedOn.load() < 150);
  assert(millis()-switchedOn.load() >= 200);
  // If the timer fires before the Arduino task resumes, no late turn-on.
  auto previousOn=switchedOn.load();
  delayStart=true;
  assert(!runDosingCountdown(1,30,"ph_down"));
  assert(switchedOn.load()==previousOn);
  assert(!pumpRunning);
  // A timer allocation failure also fails closed.
  delayStart=false; failCreate=true; controllerMode=MODE_FULL_CONTROL;
  assert(!runDosingCountdown(1,100,"ph_down"));
  assert(!pumpRunning);
  assert(controllerMode==MODE_MONITORING);
}
'''
    path = tmp_path / 'pump_cutoff.cpp'
    path.write_text(harness)
    binary = tmp_path / 'pump_cutoff'
    subprocess.run([compiler,'-std=c++17','-pthread',str(path),'-o',str(binary)],check=True,capture_output=True,text=True)
    subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=5)


def test_rejected_second_nutrient_pump_leaves_sequence_interrupted(tmp_path):
    compiler = shutil.which('g++')
    if not compiler:
        pytest.skip('Native C++ compiler is unavailable')
    source = (Path(__file__).resolve().parents[2] / 'embedded_system/hanas_sensor_controller/hanas_sensor_controller.ino').read_text()
    start = source.index('void runEcUpSequence(int controlCycleId, unsigned long durationMs, unsigned long mixingTimeMs) {')
    end = source.index('\nvoid startMixingPeriod(', start)
    harness = r'''
#include <cassert>
#include <string>
using String = std::string;
struct SerialStub { template<class T> void print(T) {} template<class T> void println(T) {} } Serial;
constexpr int MODE_MONITORING=0, MODE_FULL_CONTROL=1;
constexpr int EC_UP_A_PUMP_PIN=1, EC_UP_B_PUMP_PIN=2;
constexpr unsigned long EC_UP_INTER_DOSE_MIXING_DURATION_MS=60000;
int controllerMode=MODE_FULL_CONTROL, pulses=0;
bool mixingStarted=false;
void stopAllPumps() {}
void enterMonitoringMode() { controllerMode=MODE_MONITORING; }
bool runDosingCountdown(int pin,unsigned long,const String&) { assert(pin==EC_UP_A_PUMP_PIN); ++pulses; return true; }
bool markControlCycleActionStarted(int,const char* status) { return std::string(status)!="ec_up_b_dosing"; }
bool runInterDoseMixingCountdown(unsigned long) { return true; }
void startMixingPeriod(unsigned long) { mixingStarted=true; }
'''
    harness += source[start:end]
    harness += '\nint main() { runEcUpSequence(1,100,60000); assert(pulses==1); assert(controllerMode==MODE_MONITORING); assert(!mixingStarted); }'
    path = tmp_path / 'ec_sequence.cpp'
    path.write_text(harness)
    binary = tmp_path / 'ec_sequence'
    subprocess.run([compiler, '-std=c++17', str(path), '-o', str(binary)], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=5)
