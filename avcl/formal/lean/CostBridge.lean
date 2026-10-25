import Std

/-
Conditional P4 bridge. Remote cost is distinct from local accounting.
AdapterSoundness and ReceiptSoundness are explicit transition premises, not
axioms. A real adapter, operation binding, all cost sources, dedup retention,
and Python/SQL refinement must establish these premises separately.
-/
namespace AVCL.CostBridge

inductive Phase where
  | prepared | admitted | unknown | partialCost | final | aborted
  deriving DecidableEq

structure Operation where
  id : Nat
  upper : Nat
  actual : Nat
  spent : Nat
  remaining : Nat
  remoteClosed : Bool
  phase : Phase
  deriving DecidableEq

def exposure (s : Operation) := s.spent + s.remaining

def Active (p : Phase) : Prop := p = .admitted ∨ p = .unknown ∨ p = .partialCost
def EverAdmitted (p : Phase) : Prop := Active p ∨ p = .final

def Shape (s : Operation) : Prop :=
  match s.phase with
  | .prepared => s.actual = 0 ∧ s.spent = 0 ∧ s.remaining = s.upper
  | .admitted | .unknown | .partialCost => s.remaining = s.upper - s.spent
  | .final => s.remoteClosed = true ∧ s.spent = s.actual ∧ s.remaining = 0
  | .aborted => s.actual = 0 ∧ s.spent = 0 ∧ s.remaining = 0

def Valid (s : Operation) : Prop := s.actual ≤ s.upper ∧ s.spent ≤ s.actual ∧ Shape s

theorem valid_covers_actual {s : Operation} (valid : Valid s) : s.actual ≤ exposure s := by
  cases phase : s.phase <;> simp_all [Valid, Shape, exposure] <;> omega

-- The upper bound includes principal, fees, and every permitted retry cost.
structure AdapterSoundness (s : Operation) (nextActual : Nat) : Prop where
  monotone : s.actual ≤ nextActual
  bounded : nextActual ≤ s.upper

structure PartialReceiptSoundness (s : Operation) (reported : Nat) : Prop where
  monotone : s.spent ≤ reported
  notInvented : reported ≤ s.actual

-- remoteClosed means no future cost from ANY source bound to this operation.
structure ReceiptSoundness (s : Operation) (reported : Nat) : Prop where
  completeCost : reported = s.actual
  noFutureCost : s.remoteClosed = true

inductive Step : Operation → Operation → Prop where
  | admit (s : Operation) (ready : s.phase = .prepared) :
      Step s { s with phase := .admitted }
  | timeout (s : Operation) (sent : s.phase = .admitted) :
      Step s { s with phase := .unknown }
  | remoteCharge (s : Operation) (nextActual : Nat)
      (sent : EverAdmitted s.phase) (openRemote : s.remoteClosed = false)
      (sound : AdapterSoundness s nextActual) : Step s { s with actual := nextActual }
  | remoteClose (s : Operation) (sent : EverAdmitted s.phase) :
      Step s { s with remoteClosed := true }
  | observePartial (s : Operation) (reported : Nat) (active : Active s.phase)
      (sound : PartialReceiptSoundness s reported) :
      Step s { s with spent := reported, remaining := s.upper - reported, phase := .partialCost }
  | observeFinal (s : Operation) (reported : Nat) (active : Active s.phase)
      (sound : ReceiptSoundness s reported) :
      Step s { s with spent := reported, remaining := 0, phase := .final }
  | abortPrepared (s : Operation) (ready : s.phase = .prepared) :
      Step s { s with remaining := 0, phase := .aborted }
  | retain (s : Operation) : Step s s

theorem step_valid {s t : Operation} (valid : Valid s) (step : Step s t) : Valid t := by
  cases step with
  | admit ready => simp_all [Valid, Shape]
  | timeout sent => simp_all [Valid, Shape]
  | remoteCharge next sent openRemote sound =>
      rcases sound with ⟨monotone, bounded⟩
      cases phase : s.phase <;> simp_all [Valid, Shape, Active, EverAdmitted] <;> omega
  | remoteClose sent =>
      cases phase : s.phase <;> simp_all [Valid, Shape, Active, EverAdmitted]
  | observePartial reported active sound =>
      rcases sound with ⟨monotone, notInvented⟩
      cases phase : s.phase <;> simp_all [Valid, Shape, Active] <;> omega
  | observeFinal reported active sound =>
      rcases sound with ⟨completeCost, noFutureCost⟩
      cases phase : s.phase <;> simp_all [Valid, Shape, Active]
  | abortPrepared ready => simp_all [Valid, Shape]
  | retain => exact valid

theorem step_exposure_le {s t : Operation} (valid : Valid s) (step : Step s t) :
    exposure t ≤ exposure s := by
  cases step with
  | admit => exact Nat.le_refl _
  | timeout => exact Nat.le_refl _
  | remoteCharge => exact Nat.le_refl _
  | remoteClose => exact Nat.le_refl _
  | observePartial reported active sound =>
      rcases sound with ⟨monotone, notInvented⟩
      cases phase : s.phase <;> simp_all [Valid, Shape, Active, exposure] <;> omega
  | observeFinal reported active sound =>
      have cover := valid_covers_actual valid
      simp [exposure, sound.completeCost] at cover ⊢
      exact cover
  | abortPrepared ready =>
      simp [exposure]
  | retain => exact Nat.le_refl _

theorem step_preserves_id {s t : Operation} (step : Step s t) : t.id = s.id := by
  cases step <;> rfl

def prepared (id upper : Nat) : Operation := ⟨id, upper, 0, 0, upper, false, .prepared⟩

theorem prepared_valid (id upper : Nat) : Valid (prepared id upper) := by
  simp [Valid, Shape, prepared]

def total (metric : Operation → Nat) : List Operation → Nat
  | [] => 0
  | s :: rest => metric s + total metric rest

def AllValid : List Operation → Prop
  | [] => True
  | s :: rest => Valid s ∧ AllValid rest

def Unique (ledger : List Operation) := (ledger.map Operation.id).Nodup

theorem total_append (metric : Operation → Nat) (left right : List Operation) :
    total metric (left ++ right) = total metric left + total metric right := by
  induction left with
  | nil => simp [total]
  | cons s rest ih => simp [total, ih, Nat.add_assoc]

theorem valid_append (left right : List Operation) :
    AllValid (left ++ right) ↔ AllValid left ∧ AllValid right := by
  induction left with
  | nil => simp [AllValid]
  | cons s rest ih => simp [AllValid, ih, and_assoc]

theorem P4_cost_bridge {ledger : List Operation} (valid : AllValid ledger) :
    total Operation.actual ledger ≤ total exposure ledger := by
  induction ledger with
  | nil => exact Nat.le_refl _
  | cons s rest ih =>
      have head := valid_covers_actual valid.1
      have tail := ih valid.2
      simp only [total]
      omega

inductive LedgerStep (limit : Nat) : List Operation → List Operation → Prop where
  | prepare (ledger : List Operation) (id upper : Nat)
      (fresh : id ∉ ledger.map Operation.id)
      (guard : total exposure ledger + upper ≤ limit) :
      LedgerStep limit ledger (prepared id upper :: ledger)
  | update (left right : List Operation) (s t : Operation) (step : Step s t) :
      LedgerStep limit (left ++ s :: right) (left ++ t :: right)

theorem ledger_step_preserves {limit before after}
    (valid : AllValid before) (unique : Unique before)
    (budget : total exposure before ≤ limit) (step : LedgerStep limit before after) :
    AllValid after ∧ Unique after ∧ total exposure after ≤ limit := by
  cases step with
  | prepare ledger id upper fresh guard =>
      refine ⟨⟨prepared_valid id upper, valid⟩, ?_, ?_⟩
      · exact List.nodup_cons.mpr ⟨fresh, unique⟩
      · simpa [total, prepared, exposure, Nat.add_comm] using guard
  | update left right s t step =>
      rw [valid_append] at valid
      have localValid := step_valid valid.2.1 step
      have localBound := step_exposure_le valid.2.1 step
      refine ⟨(valid_append left (t :: right)).mpr ⟨valid.1, localValid, valid.2.2⟩, ?_, ?_⟩
      · simpa only [Unique, List.map_append, List.map_cons, step_preserves_id step] using unique
      · rw [total_append] at budget ⊢
        simp only [total] at budget ⊢
        omega

inductive Run (limit : Nat) (initial : List Operation) : List Operation → Prop where
  | start : Run limit initial initial
  | next {before after} : Run limit initial before → LedgerStep limit before after → Run limit initial after

theorem run_invariants {limit initial final}
    (valid : AllValid initial) (unique : Unique initial) (budget : total exposure initial ≤ limit)
    (run : Run limit initial final) :
    AllValid final ∧ Unique final ∧ total exposure final ≤ limit := by
  induction run with
  | start => exact ⟨valid, unique, budget⟩
  | next _ step ih => exact ledger_step_preserves ih.1 ih.2.1 ih.2.2 step

theorem P4_actual_within_budget {limit initial final}
    (valid : AllValid initial) (unique : Unique initial) (budget : total exposure initial ≤ limit)
    (run : Run limit initial final) :
    total Operation.actual final ≤ total exposure final ∧ total exposure final ≤ limit ∧ Unique final := by
  have facts := run_invariants valid unique budget run
  exact ⟨P4_cost_bridge facts.1, facts.2.2, facts.2.1⟩

-- A concrete nonempty execution: effect, lost receipt, partial observation,
-- delayed fee, remote closure, and authoritative final reconciliation.
def w0 := prepared 7 3
def w1 : Operation := { w0 with phase := .admitted }
def w2 : Operation := { w1 with actual := 2 }
def w3 : Operation := { w2 with phase := .unknown }
def w4 : Operation := { w3 with spent := 2, remaining := 1, phase := .partialCost }
def w5 : Operation := { w4 with actual := 3 }
def w6 : Operation := { w5 with remoteClosed := true }
def w7 : Operation := { w6 with spent := 3, remaining := 0, phase := .final }

theorem singleton_next {limit s t} (run : Run limit [] [s]) (step : Step s t) :
    Run limit [] [t] := Run.next run (LedgerStep.update [] [] s t step)

theorem example_partial_then_final : Run 3 [] [w7] ∧ w7.actual = 3 ∧ exposure w7 = 3 := by
  have r0 : Run 3 [] [w0] := Run.next Run.start
    (LedgerStep.prepare [] 7 3 (by simp) (by decide))
  have r1 : Run 3 [] [w1] := singleton_next r0 (Step.admit w0 rfl)
  have r2 : Run 3 [] [w2] := singleton_next r1
    (Step.remoteCharge w1 2 (by simp [EverAdmitted, Active, w1]) rfl ⟨by decide, by decide⟩)
  have r3 : Run 3 [] [w3] := singleton_next r2 (Step.timeout w2 rfl)
  have r4 : Run 3 [] [w4] := singleton_next r3
    (Step.observePartial w3 2 (by simp [Active, w3]) ⟨by decide, by decide⟩)
  have r5 : Run 3 [] [w5] := singleton_next r4
    (Step.remoteCharge w4 3 (by simp [EverAdmitted, Active, w4]) rfl ⟨by decide, by decide⟩)
  have r6 : Run 3 [] [w6] := singleton_next r5
    (Step.remoteClose w5 (by simp [EverAdmitted, Active, w5, w4]))
  have r7 : Run 3 [] [w7] := singleton_next r6
    (Step.observeFinal w6 3 (by simp [Active, w6, w5, w4]) ⟨rfl, rfl⟩)
  exact ⟨r7, rfl, rfl⟩

-- Counterexample to deriving real-cost safety from ledger safety alone.
def releasedTooSoon : Operation := { w3 with remaining := 0 }
theorem example_local_budget_insufficient :
    exposure releasedTooSoon ≤ 3 ∧ releasedTooSoon.actual > exposure releasedTooSoon := by decide

theorem example_closed_blocks_charge : ¬ (w7.remoteClosed = false) := by decide

#print axioms P4_cost_bridge
#print axioms P4_actual_within_budget
#print axioms step_valid
#print axioms example_partial_then_final
#print axioms example_local_budget_insufficient
#print axioms example_closed_blocks_charge

end AVCL.CostBridge
