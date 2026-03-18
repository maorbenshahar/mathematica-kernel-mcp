(* ::Package:: *)

(* ::Title:: *)
(*Dimensional Regularization ODE*)

(* ::Text:: *)
(*Explore the radial ODE with dimensional dependence carried by e.*)

(* ::Section:: *)
(*Setup*)

(* ::Input:: *)
ClearAll[v, r, e, l, w, eqn, generalSolution, solutionE0, solutionEHalf, verificationE0, verificationEHalf]

(* ::Input:: *)
eqn = v''[r] + (w^2 - l^2 w^2/r^(1 - 2 e) - (l - e) (l - e + 1)/r^2) v[r] == 0

(* ::Section:: *)
(*General Solve Attempt*)

(* ::Input:: *)
generalSolution = DSolveValue[eqn, v, r]

(* ::Section:: *)
(*Special Dimensions*)

(* ::Input:: *)
solutionE0 = DSolveValue[eqn /. e -> 0, v, r]

(* ::Input:: *)
solutionEHalf = DSolveValue[eqn /. e -> 1/2, v, r]

(* ::Input:: *)
verificationE0 = FullSimplify[eqn /. e -> 0 /. v -> solutionE0]

(* ::Input:: *)
verificationEHalf = FullSimplify[eqn /. e -> 1/2 /. v -> solutionEHalf]
