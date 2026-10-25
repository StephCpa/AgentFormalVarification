import Std

/-
Abstract online delegation with a single shared ledger. Permissions are
predicates; authentication and current revocation state are supplied inputs.
This file does not prove the Python checker, SQL transactions, cryptography,
remote cost bounds, liveness, process isolation, or offline escrow.
-/
namespace AVCL.Delegation

structure Capability where
  id : Nat
  tenant : Nat
  subject : Nat
  orders : Nat → Prop
  tools : Nat → Prop
  effects : Nat → Prop
  maxAmount : Nat
  expires : Nat
  quota : Nat
  epoch : Nat
  mayDelegate : Bool

def Attenuates (child parent : Capability) : Prop :=
  child.tenant = parent.tenant ∧ child.epoch = parent.epoch ∧
  (∀ x, child.orders x → parent.orders x) ∧
  (∀ x, child.tools x → parent.tools x) ∧
  (∀ x, child.effects x → parent.effects x) ∧
  child.maxAmount ≤ parent.maxAmount ∧ child.expires ≤ parent.expires ∧
  child.quota ≤ parent.quota

theorem attenuates_refl (c : Capability) : Attenuates c c := by
  exact ⟨rfl, rfl, fun _ h => h, fun _ h => h, fun _ h => h,
    Nat.le_refl _, Nat.le_refl _, Nat.le_refl _⟩

theorem attenuates_trans {a b c : Capability}
    (ab : Attenuates a b) (bc : Attenuates b c) : Attenuates a c := by
  rcases ab with ⟨tenantAB, ae, ao, au, af, am, ax, aq⟩
  rcases bc with ⟨bt, be, bo, bu, bf, bm, bx, bq⟩
  exact ⟨tenantAB.trans bt, ae.trans be, fun x h => bo x (ao x h),
    fun x h => bu x (au x h), fun x h => bf x (af x h),
    Nat.le_trans am bm, Nat.le_trans ax bx, Nat.le_trans aq bq⟩

-- Paths are leaf first. Fresh IDs and subjects rule out cycles; the model
-- supports any finite depth, including the implementation's limit of eight.
inductive Chain (root : Capability) : Capability → List Capability → Prop where
  | root : Chain root root [root]
  | extend {parent path} (prior : Chain root parent path) (child : Capability)
      (allowed : parent.mayDelegate = true) (subset : Attenuates child parent)
      (freshId : child.id ∉ path.map Capability.id)
      (freshSubject : child.subject ∉ path.map Capability.subject) :
      Chain root child (child :: path)

theorem root_member {root leaf path} (chain : Chain root leaf path) : root ∈ path := by
  induction chain with
  | root => simp
  | extend _ _ _ _ _ _ ih => exact List.mem_cons_of_mem _ ih

theorem chain_ids_unique {root leaf path} (chain : Chain root leaf path) :
    (path.map Capability.id).Nodup := by
  induction chain with
  | root => simp
  | extend _ _ _ _ fresh _ ih => exact List.nodup_cons.mpr ⟨fresh, ih⟩

theorem chain_subjects_unique {root leaf path} (chain : Chain root leaf path) :
    (path.map Capability.subject).Nodup := by
  induction chain with
  | root => simp
  | extend _ _ _ _ _ fresh ih => exact List.nodup_cons.mpr ⟨fresh, ih⟩

theorem P7_scope_transitive {root leaf path} (chain : Chain root leaf path) :
    ∀ ancestor ∈ path, Attenuates leaf ancestor := by
  induction chain with
  | root =>
      intro ancestor member
      have eq : ancestor = root := by simpa using member
      subst ancestor
      exact attenuates_refl root
  | extend _ child _ subset _ _ ih =>
      intro ancestor member
      rcases List.mem_cons.mp member with eq | old
      · subst ancestor; exact attenuates_refl child
      · exact attenuates_trans subset (ih ancestor old)

def Live (now epoch : Nat) (revoked active : Nat → Bool) (c : Capability) : Prop :=
  revoked c.id = false ∧ active c.subject = true ∧ c.epoch = epoch ∧ now < c.expires

def ChainLive (now epoch : Nat) (revoked active : Nat → Bool)
    (path : List Capability) : Prop := ∀ c ∈ path, Live now epoch revoked active c

structure Request where
  tenant : Nat
  subject : Nat
  order : Nat
  tool : Nat
  effect : Nat
  amount : Nat

def CanAdmit (root leaf : Capability) (path : List Capability) (r : Request)
    (now epoch : Nat) (revoked active : Nat → Bool) : Prop :=
  Chain root leaf path ∧ r.tenant = leaf.tenant ∧ r.subject = leaf.subject ∧
  ChainLive now epoch revoked active path ∧ leaf.orders r.order ∧
  leaf.tools r.tool ∧ leaf.effects r.effect ∧ r.amount ≤ leaf.maxAmount

theorem P7_ancestor_revocation {root leaf path r now epoch revoked active ancestor}
    (member : ancestor ∈ path) (revokedAncestor : revoked ancestor.id = true) :
    ¬ CanAdmit root leaf path r now epoch revoked active := by
  intro admitted
  have live := admitted.2.2.2.1 ancestor member
  have clear := live.1
  rw [revokedAncestor] at clear
  contradiction

theorem revoked_root_blocks_descendant {root leaf path r now epoch revoked active}
    (revokedRoot : revoked root.id = true) :
    ¬ CanAdmit root leaf path r now epoch revoked active := by
  intro admitted
  exact P7_ancestor_revocation (root_member admitted.1) revokedRoot admitted

-- One entry per logical operation, with a frozen, checked ancestor path.
-- spent includes all modeled fees; remaining is still-possible exposure.
structure Entry (root : Capability) where
  operation : Nat
  leaf : Capability
  path : List Capability
  chain : Chain root leaf path
  spent : Nat
  remaining : Nat

def Entry.exposure {root} (e : Entry root) := e.spent + e.remaining
abbrev Entry.uses {root} (e : Entry root) (account : Nat) := account ∈ e.path.map Capability.id

def load {root} (account : Nat) : List (Entry root) → Nat
  | [] => 0
  | e :: rest => (if e.uses account then e.exposure else 0) + load account rest

def total {root} : List (Entry root) → Nat
  | [] => 0
  | e :: rest => e.exposure + total rest

def Safe {root} (limits : Nat → Nat) (ledger : List (Entry root)) : Prop :=
  ∀ account, load account ledger ≤ limits account

def Unique {root} (ledger : List (Entry root)) : Prop :=
  (ledger.map Entry.operation).Nodup

-- Budget IDs must resolve to the immutable quota declared by each capability.
def Bound {root} (limits : Nat → Nat) : List (Entry root) → Prop
  | [] => True
  | e :: rest => (∀ c ∈ e.path, limits c.id = c.quota) ∧ Bound limits rest

theorem bound_append {root} (limits : Nat → Nat) (xs ys : List (Entry root)) :
    Bound limits (xs ++ ys) ↔ Bound limits xs ∧ Bound limits ys := by
  induction xs with
  | nil => simp [Bound]
  | cons e rest ih => simp [Bound, ih, and_assoc]

theorem bound_member {root limits} {ledger : List (Entry root)} (bound : Bound limits ledger) :
    ∀ e ∈ ledger, ∀ c ∈ e.path, limits c.id = c.quota := by
  induction ledger with
  | nil => simp
  | cons e rest ih =>
      intro entry member
      rcases List.mem_cons.mp member with eq | old
      · subst entry; exact bound.1
      · exact ih bound.2 entry old

theorem root_counts_once {root} (ledger : List (Entry root)) :
    load root.id ledger = total ledger := by
  induction ledger with
  | nil => rfl
  | cons e rest ih =>
      have member : e.uses root.id := List.mem_map.mpr ⟨root, root_member e.chain, rfl⟩
      simp only [load, total, if_pos member, ih]

theorem load_append {root} (a : Nat) (xs ys : List (Entry root)) :
    load a (xs ++ ys) = load a xs + load a ys := by
  induction xs with
  | nil => simp [load]
  | cons e rest ih => simp [load, ih, Nat.add_assoc]

def resolve {root} (e : Entry root) (spent remaining : Nat) : Entry root :=
  { e with spent := spent, remaining := remaining }

theorem load_resolution_le {root} (a : Nat) (left right : List (Entry root))
    (e : Entry root) (spent remaining : Nat) (covered : spent + remaining ≤ e.exposure) :
    load a (left ++ resolve e spent remaining :: right) ≤ load a (left ++ e :: right) := by
  rw [load_append, load_append]
  by_cases used : a ∈ e.path.map Capability.id
  · simp [load, resolve, Entry.uses, Entry.exposure, used] at *
    omega
  · simp [load, resolve, Entry.uses, used]

inductive Step {root} (limits : Nat → Nat) : List (Entry root) → List (Entry root) → Prop where
  | reserve (ledger : List (Entry root)) (e : Entry root)
      (fresh : e.operation ∉ ledger.map Entry.operation) (uncharged : e.spent = 0)
      (binding : ∀ c ∈ e.path, limits c.id = c.quota)
      (guard : ∀ a, e.uses a → load a ledger + e.exposure ≤ limits a) :
      Step limits ledger (e :: ledger)
  | settle (left right : List (Entry root)) (e : Entry root) (spent remaining : Nat)
      (monotone : e.spent ≤ spent) (covered : spent + remaining ≤ e.exposure) :
      Step limits (left ++ e :: right) (left ++ resolve e spent remaining :: right)
  -- Delegate/Unknown/retry/revoke/handoff do not create or release exposure.
  | retain (ledger : List (Entry root)) : Step limits ledger ledger

theorem step_safe {root limits} {before after : List (Entry root)}
    (safe : Safe limits before) (step : Step limits before after) : Safe limits after := by
  intro a
  cases step with
  | reserve ledger e _ _ _ guard =>
      by_cases used : e.uses a
      · simp only [load, if_pos used]; have h := guard a used; omega
      · simpa only [load, if_neg used, Nat.zero_add] using safe a
  | settle left right e spent remaining _ covered =>
      exact Nat.le_trans (load_resolution_le a left right e spent remaining covered) (safe a)
  | retain => exact safe a

theorem step_unique {root limits} {before after : List (Entry root)}
    (unique : Unique before) (step : Step limits before after) : Unique after := by
  cases step with
  | reserve ledger e fresh _ _ _ => exact List.nodup_cons.mpr ⟨fresh, unique⟩
  | settle left right e spent remaining _ _ =>
      simpa only [Unique, List.map_append, List.map_cons, resolve] using unique
  | retain => exact unique

theorem step_bound {root limits} {before after : List (Entry root)}
    (bound : Bound limits before) (step : Step limits before after) : Bound limits after := by
  cases step with
  | reserve ledger e _ _ binding _ => exact ⟨binding, bound⟩
  | settle left right e spent remaining _ _ =>
      rw [bound_append] at bound ⊢
      exact bound
  | retain => exact bound

inductive Run {root} (limits : Nat → Nat) (initial : List (Entry root)) : List (Entry root) → Prop where
  | start : Run limits initial initial
  | next {before after} : Run limits initial before → Step limits before after → Run limits initial after

theorem run_invariants {root limits} {initial final : List (Entry root)}
    (safe : Safe limits initial) (unique : Unique initial) (run : Run limits initial final) :
    Safe limits final ∧ Unique final := by
  induction run with
  | start => exact ⟨safe, unique⟩
  | next _ step ih => exact ⟨step_safe ih.1 step, step_unique ih.2 step⟩

theorem P7_shared_budget {root limits} {initial final : List (Entry root)}
    (safe : Safe limits initial) (unique : Unique initial) (run : Run limits initial final) :
    total final ≤ limits root.id ∧ Unique final := by
  have facts := run_invariants safe unique run
  exact ⟨by rw [← root_counts_once]; exact facts.1 root.id, facts.2⟩

theorem P7_composition {root limits} {initial final : List (Entry root)}
    (safe : Safe limits initial) (unique : Unique initial) (bound : Bound limits initial)
    (rootBudget : limits root.id = root.quota) (run : Run limits initial final) :
    (∀ e ∈ final, Attenuates e.leaf root) ∧
    (∀ e ∈ final, ∀ ancestor ∈ e.path, load ancestor.id final ≤ ancestor.quota) ∧
    total final ≤ root.quota ∧ Unique final := by
  have finalBound : Bound limits final := by
    induction run with
    | start => exact bound
    | next _ step ih => exact step_bound ih step
  have facts := run_invariants safe unique run
  refine ⟨fun e _ => P7_scope_transitive e.chain root (root_member e.chain), ?_, ?_, facts.2⟩
  · intro e member ancestor inPath
    rw [← bound_member finalBound e member ancestor inPath]
    exact facts.1 ancestor.id
  · rw [← rootBudget]
    exact (P7_shared_budget safe unique run).1

-- Concrete nonempty witnesses: root quota 100, two child quotas 80 each.
-- An initial reservation of 60 is legal, but another 60 exceeds the root.
def exampleRoot : Capability :=
  ⟨0, 0, 0, (fun x => x = 1 ∨ x = 2), (fun x => x = 0),
    (fun x => x = 0), 100, 1000, 100, 0, true⟩

def exampleChildOne : Capability :=
  { exampleRoot with
    id := 1
    subject := 1
    orders := fun x => x = 1
    quota := 80
    expires := 800
    mayDelegate := false }

def exampleChildTwo : Capability :=
  { exampleRoot with
    id := 2
    subject := 2
    orders := fun x => x = 2
    quota := 80
    expires := 800
    mayDelegate := false }

theorem example_chain_one : Chain exampleRoot exampleChildOne [exampleChildOne, exampleRoot] := by
  apply Chain.extend Chain.root exampleChildOne rfl
  · simp [Attenuates, exampleRoot, exampleChildOne]
  · simp [exampleRoot, exampleChildOne]
  · simp [exampleRoot, exampleChildOne]

theorem example_chain_two : Chain exampleRoot exampleChildTwo [exampleChildTwo, exampleRoot] := by
  apply Chain.extend Chain.root exampleChildTwo rfl
  · simp [Attenuates, exampleRoot, exampleChildTwo]
  · simp [exampleRoot, exampleChildTwo]
  · simp [exampleRoot, exampleChildTwo]

def exampleOne : Entry exampleRoot :=
  ⟨10, exampleChildOne, [exampleChildOne, exampleRoot], example_chain_one, 0, 60⟩

def exampleTwo : Entry exampleRoot :=
  ⟨20, exampleChildTwo, [exampleChildTwo, exampleRoot], example_chain_two, 0, 60⟩

def exampleLimits (a : Nat) := if a = 0 then 100 else if a = 1 ∨ a = 2 then 80 else 0

theorem example_nonempty_run : Run exampleLimits [] [exampleOne] := by
  apply Run.next Run.start
  apply Step.reserve [] exampleOne
  · simp
  · rfl
  · intro c member
    simp [exampleOne] at member
    rcases member with rfl | rfl <;> rfl
  · intro a member
    simp [Entry.uses, exampleOne, exampleChildOne, exampleRoot] at member
    rcases member with rfl | rfl <;> decide

theorem example_root_bound : total [exampleOne] ≤ exampleRoot.quota := by
  have composed := P7_composition (root := exampleRoot) (initial := []) (final := [exampleOne])
    (limits := exampleLimits) (by simp [Safe, load])
    (by simp [Unique]) True.intro rfl example_nonempty_run
  exact composed.2.2.1

theorem example_rejects_second_reservation :
    ¬ (∀ a, exampleTwo.uses a → load a [exampleOne] + exampleTwo.exposure ≤ exampleLimits a) := by
  intro allowed
  have bad := allowed 0 (by simp [Entry.uses, exampleTwo, exampleChildTwo, exampleRoot])
  simp [load, Entry.uses, Entry.exposure, exampleOne, exampleTwo,
    exampleChildOne, exampleChildTwo, exampleRoot, exampleLimits] at bad

theorem example_partial_then_final :
    Run exampleLimits [] [resolve (resolve exampleOne 20 40) 60 0] := by
  apply Run.next (Run.next example_nonempty_run (Step.settle [] [] exampleOne 20 40 (by decide) (by decide)))
  exact Step.settle [] [] (resolve exampleOne 20 40) 60 0 (by decide) (by decide)

def exampleRequest : Request := ⟨0, 1, 1, 0, 0, 60⟩

theorem example_can_admit : CanAdmit exampleRoot exampleChildOne [exampleChildOne, exampleRoot]
    exampleRequest 1 0 (fun _ => false) (fun _ => true) := by
  refine ⟨example_chain_one, rfl, rfl, ?_, rfl, rfl, rfl, by decide⟩
  intro c member
  rcases List.mem_cons.mp member with eq | old
  · subst c; simp [Live, exampleChildOne, exampleRoot]
  · have eq : c = exampleRoot := by simpa using old
    subst c; simp [Live, exampleRoot]

theorem example_root_revoked_leaf_not_revoked :
    (exampleChildOne.id == 0) = false ∧
    ¬ CanAdmit exampleRoot exampleChildOne [exampleChildOne, exampleRoot]
      exampleRequest 1 0 (fun id => id == 0) (fun _ => true) := by
  exact ⟨by decide, revoked_root_blocks_descendant rfl⟩

#print axioms P7_scope_transitive
#print axioms P7_ancestor_revocation
#print axioms P7_shared_budget
#print axioms P7_composition
#print axioms example_nonempty_run
#print axioms example_rejects_second_reservation
#print axioms example_partial_then_final
#print axioms example_can_admit
#print axioms example_root_revoked_leaf_not_revoked

end AVCL.Delegation
