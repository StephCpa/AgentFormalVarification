(set-option :timeout 5000)
(set-option :rlimit 1000000)
(set-option :smt.random_seed 0)
; benchmark generated from python API
(set-info :status unknown)
(assert
 (let (($x6 (and true)))
(and $x6 true false)))
(check-sat)
