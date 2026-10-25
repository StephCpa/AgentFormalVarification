import Std

/-
Small transition core for P1/P2/P3. The Authorized predicate is abstract: this
does not prove cryptography, signatures, policy evaluation, Python refinement,
SQL serializability or crash recovery. All real-world rely conditions remain
outside this theorem. No axiom or sorry is introduced here.
-/
namespace AVCL

structure Account where
  spent : Nat
  pending : Nat
  limit : Nat

def Account.Safe (a : Account) : Prop := a.spent + a.pending ≤ a.limit

def reserve (a : Account) (upper : Nat) : Account :=
  { a with pending := a.pending + upper }

theorem reserve_safe (a : Account) (upper : Nat)
    (guard : a.spent + a.pending + upper ≤ a.limit) :
    (reserve a upper).Safe := by
  unfold Account.Safe reserve
  dsimp
  omega

-- Replace one old reservation by newly realized cost and residual exposure.
-- Final settlement has residual = 0. Unknown is the unchanged state.
def resolve (a : Account) (old charge residual : Nat) : Account :=
  { a with spent := a.spent + charge, pending := a.pending - old + residual }

theorem resolve_safe (a : Account) (old charge residual : Nat)
    (safe : a.Safe) (held : old ≤ a.pending)
    (covered : charge + residual ≤ old) :
    (resolve a old charge residual).Safe := by
  unfold Account.Safe at *
  unfold resolve
  dsimp
  omega

abbrev Accounts (n : Nat) := Fin n → Account
def AllSafe {n : Nat} (accounts : Accounts n) : Prop := ∀ i, (accounts i).Safe

-- The guard ranges over every account BEFORE this single transition.
inductive BudgetStep {n : Nat} : Accounts n → Accounts n → Prop where
  | reserveAll (s : Accounts n) (upper : Fin n → Nat)
      (guard : ∀ i, (s i).spent + (s i).pending + upper i ≤ (s i).limit) :
      BudgetStep s (fun i => reserve (s i) (upper i))
  | resolveAll (s : Accounts n) (old charge residual : Fin n → Nat)
      (held : ∀ i, old i ≤ (s i).pending)
      (covered : ∀ i, charge i + residual i ≤ old i) :
      BudgetStep s (fun i => resolve (s i) (old i) (charge i) (residual i))
  | unknown (s : Accounts n) : BudgetStep s s

theorem step_preserves_budget {n : Nat} {s t : Accounts n}
    (safe : AllSafe s) (step : BudgetStep s t) : AllSafe t := by
  cases step with
  | reserveAll upper guard => exact fun i => reserve_safe (s i) (upper i) (guard i)
  | resolveAll old charge residual held covered =>
      exact fun i => resolve_safe (s i) (old i) (charge i) (residual i) (safe i) (held i) (covered i)
  | unknown => exact safe

inductive BudgetRun {n : Nat} (initial : Accounts n) : Accounts n → Prop where
  | start : BudgetRun initial initial
  | next {s t : Accounts n} : BudgetRun initial s → BudgetStep s t → BudgetRun initial t

theorem P3_budget_preserved {n : Nat} {initial final : Accounts n}
    (safe : AllSafe initial) (run : BudgetRun initial final) : AllSafe final := by
  induction run with
  | start => exact safe
  | next _ step ih => exact step_preserves_budget ih step

-- Events record authorization at the admission point, including its epoch.
structure Admission where
  permit : Nat
  epoch : Nat
  deriving DecidableEq

structure AdmissionState where
  epoch : Nat
  history : List Admission

def Used (s : AdmissionState) (p : Nat) : Prop := p ∈ s.history.map Admission.permit
def Unique (s : AdmissionState) : Prop := (s.history.map Admission.permit).Nodup
def AuthorizedHistory (authorized : Nat → Nat → Prop) (s : AdmissionState) : Prop :=
  ∀ event ∈ s.history, authorized event.epoch event.permit

inductive AdmissionStep (authorized : Nat → Nat → Prop) : AdmissionState → AdmissionState → Prop where
  | admit (s : AdmissionState) (p : Nat) (fresh : ¬ Used s p)
      (valid : authorized s.epoch p) :
      AdmissionStep authorized s ⟨s.epoch, ⟨p, s.epoch⟩ :: s.history⟩
  | revoke (s : AdmissionState) :
      AdmissionStep authorized s ⟨s.epoch + 1, s.history⟩
  | retry (s : AdmissionState) : AdmissionStep authorized s s

theorem admission_unique {authorized : Nat → Nat → Prop} {s t : AdmissionState}
    (unique : Unique s) (step : AdmissionStep authorized s t) : Unique t := by
  cases step with
  | admit p fresh valid =>
      simp only [Unique, List.map_cons, List.nodup_cons]
      exact ⟨fresh, unique⟩
  | revoke => exact unique
  | retry => exact unique

theorem admission_authorized {authorized : Nat → Nat → Prop} {s t : AdmissionState}
    (safe : AuthorizedHistory authorized s) (step : AdmissionStep authorized s t) :
    AuthorizedHistory authorized t := by
  cases step with
  | admit p fresh valid =>
      intro event member
      simp only [List.mem_cons] at member
      rcases member with rfl | old
      · exact valid
      · exact safe event old
  | revoke => exact safe
  | retry => exact safe

inductive AdmissionRun (authorized : Nat → Nat → Prop) (initial : AdmissionState) : AdmissionState → Prop where
  | start : AdmissionRun authorized initial initial
  | next {s t : AdmissionState} : AdmissionRun authorized initial s → AdmissionStep authorized s t →
      AdmissionRun authorized initial t

theorem P1_P2_admission {authorized : Nat → Nat → Prop} {s t : AdmissionState}
    (initialUnique : Unique s) (initialAuthorized : AuthorizedHistory authorized s)
    (run : AdmissionRun authorized s t) : Unique t ∧ AuthorizedHistory authorized t := by
  induction run with
  | start => exact ⟨initialUnique, initialAuthorized⟩
  | next _ step ih => exact ⟨admission_unique ih.1 step, admission_authorized ih.2 step⟩

#print axioms P3_budget_preserved
#print axioms P1_P2_admission

end AVCL
