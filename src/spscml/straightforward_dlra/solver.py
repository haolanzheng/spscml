import jax.numpy as jnp
import jax
import equinox as eqx
from diffrax import diffeqsolve, Euler, Dopri5, ODETerm, PIDController, RESULTS, SaveAt

from jaxtyping import PyTree

from ..plasma import TwoSpeciesPlasma
from ..rk import rk1, ssprk2
from ..muscl import slope_limited_flux, slope_limited_flux_divergence
from ..poisson import poisson_solve
from ..collisions_and_sources import collision_frequency_shape_func, flux_source_shape_func, maxwellian
from .poisson import solve_poisson_KV, solve_poisson_XSV, solve_poisson_XL
from ..utils import zeroth_moment
from ..collisions_and_sources import maxwellianv, collision_frequency_shape_func

SPECIES = ['electron', 'ion']

SCHEME = 'upwind'

class Solver(eqx.Module):
    """
    Straightforward Dynamical Low-Rank Approximation (DLRA) solver for the Vlasov-BGK equation.
    
    This solver uses the KSL splitting approach for the DLRA method, where the solution is decomposed
    into low-rank factors X, S, V and evolved using separate steps for K, S, and L components.

    The X and V factors are both expected to be "wide": that is, they have shapes
    
        X.shape == (r, Nx)
        V.shape == (r, Nv)

    The full distribution function can be recovered by multiplying through:

        f = X.T @ S @ V

    
    Attributes:
        plasma: Two-species plasma configuration
        r: Rank of the low-rank approximation
        grids: Dictionary containing spatial and phase space grids
        boundary_type: Type of boundary conditions ('AbsorbingWall' or 'Periodic')
        As: Dictionary of mass ratios for each species
        Zs: Dictionary of charge numbers for each species
        nus: Dictionary of collision frequencies for each species
    """
    plasma: TwoSpeciesPlasma
    r: int
    grids: PyTree
    boundary_type: str
    As: dict
    Zs: dict
    nus: dict


    def __init__(self,
                 plasma: TwoSpeciesPlasma, 
                 r: int,
                 grids,
                 nu_ee, nu_ii,
                 boundary_type='AbsorbingWall'):
        """
        Initialize the DLRA solver.
        
        Args:
            plasma: Two-species plasma configuration
            r: Rank of the low-rank approximation
            grids: Dictionary containing spatial and phase space grids
            nu_ee: Electron-electron collision frequency
            nu_ii: Ion-ion collision frequency
            boundary_type: Type of boundary conditions ('AbsorbingWall' or 'Periodic')
        """
        self.plasma = plasma
        self.r = r
        self.grids = grids
        self.boundary_type = boundary_type
        self.As = {'electron': self.plasma.Ae, 'ion': self.plasma.Ai}
        self.Zs = {'electron': self.plasma.Ze, 'ion': self.plasma.Zi}
        freq_shape_func = collision_frequency_shape_func(self.grids)
        nu_ee *= freq_shape_func
        nu_ii *= freq_shape_func
        self.nus = {'electron': nu_ee, 'ion': nu_ii}
        


    def solve(self, dt, Nt, initial_conditions, boundary_conditions, dtmax):
        """
        Solve the Vlasov-BGK equation using the DLRA method.
        
        Args:
            dt: Time step size
            Nt: Number of time steps
            initial_conditions: Dictionary of initial condition functions for each species
            boundary_conditions: Dictionary of boundary condition specifications
            dtmax: Maximum time step (for adaptive stepping)
            
        Returns:
            Solution object containing all saved timesteps with low-rank factors (X, S, V) for each species
        """
        y0 = {
            'electron': initial_conditions['electron'](*self.grids['electron'].xv),
            'ion': initial_conditions['ion'](*self.grids['ion'].xv),
        }

        solution = diffeqsolve(
            terms=ODETerm(self.step),
            solver=Stepper(),
            t0=0.0,
            t1=Nt*dt,
            max_steps=Nt + 4,
            dt0=dt,
            y0=y0,
            args={"dt": dt, 'bcs': boundary_conditions},
            saveat=SaveAt(ts=jnp.linspace(0.0, Nt*dt, 100)),
        )

        # Return the full solution with all timesteps
        return solution


    def step(self, t, ys, args):
        """
        Perform one time step using KSL splitting.
        
        Args:
            t: Current time
            ys: Current state (XSV decomposition for each species)
            args: Additional arguments containing dt, boundary conditions, etc.
            
        Returns:
            Updated state after one time step
        """
        dt = args['dt']
        ys = self.step_K(t, ys, args)
        ys = self.step_S(t, ys, args)
        ys = self.step_L(t, ys, args)
        return ys


    def step_K(self, t, ys, args):
        """
        Perform K-step of the KSL splitting (evolve X^T S component).
        
        Args:
            t: Current time
            ys: Current state (XSV decomposition for each species)
            args: Additional arguments
            
        Returns:
            Updated state after K-step
        """
        K_of = lambda X, S, V: (X.T @ S).T

        flux_out = self.ion_flux_out(ys)
        
        args_of = lambda sp: {**args, 'V': ys[sp][2], 'Z': self.Zs[sp], 'A': self.As[sp], 'nu': self.nus[sp],
                              'flux_out': flux_out}

        def step_Ks_with_E_RHS(Ks):
            # HACKATHON: E = ...
            E = solve_poisson_KV(Ks, ys, self.grids, args['bcs'], self.plasma)
            args['E'] = E
            return { sp: self.K_step_single_species_RHS(Ks[sp], self.grids[sp], 
                                                                 {**args_of(sp)})
                    for sp in SPECIES }


        Ks = rk1({ sp: K_of(*ys[sp]) for sp in SPECIES }, step_Ks_with_E_RHS, args['dt'])

        def XSV_of(K, y, grid):
            _, _, V = y
            Xt, S = jnp.linalg.qr(K.T)
            return (Xt.T / grid.dx**0.5, S * grid.dx**0.5, V)

        return { sp: XSV_of(Ks[sp], ys[sp], self.grids[sp]) for sp in SPECIES }


    def K_step_single_species_RHS(self, K, grid, args):
        """
        Compute right-hand side for K-step for a single species.
        
        Args:
            K: Current K matrix (X^T S)
            grid: Phase space grid for the species
            args: Arguments containing V, E, collision parameters, etc.
            
        Returns:
            Time derivative of K matrix
        """
        V = args['V']
        v = grid.vs
        r = self.r
        assert V.shape == (r, grid.Nv)

        v_plus_matrix = V @ jnp.diag(jnp.where(v > 0, v, 0.0)) @ V.T * grid.dv
        v_minus_matrix = V @ jnp.diag(jnp.where(v < 0, v, 0.0)) @ V.T * grid.dv

        K_bcs = self.apply_K_bcs(K, V, grid, n_ghost_cells=2)
        v_flux_func = lambda left, right: v_plus_matrix @ left + v_minus_matrix @ right
        v_flux = slope_limited_flux(K_bcs, SCHEME, v_flux_func, grid.dx, axis=1)

        v_flux_diff = jnp.diff(v_flux, axis=1) / grid.dx

        # HACKATHON: add E*df/dv term here
        # You'll need to implement:
        # 1. Compute upwinded E arrays based on sign of Z/A * E
        # 2. Compute upwind dV/dv matrices <V, D^\pm V>
        # 3. Compute the projected E*df/dv flux term

        # HACKATHON: add collision terms and flux source terms here
        # You'll need to implement:
        # 1. Compute density n
        # 2. BGK collision operator: nu * (M - f) where M is Maxwellian with density n
        # 3. Flux source terms for particle injection
        # See collision_frequency_shape_func, flux_source_shape_func, and maxwellian in collisions_and_sources.py
        E = args['E']
        Dp_V = jnp.diff(jnp.concatenate([jnp.zeros((V.shape[0], 1)), V], axis=1), axis=1) / grid.dv
        Dm_V = jnp.diff(jnp.concatenate([V, jnp.zeros((V.shape[0], 1))], axis=1), axis=1) / grid.dv
        E_plus  = jnp.where(args['Z'] * E > 0, E, 0.)
        E_minus = jnp.where(args['Z'] * E < 0, E, 0.)
        E_term1 = V @ Dp_V.T * grid.dv
        E_term2 = V @ Dm_V.T * grid.dv
        E_flux = self.plasma.omega_c_tau * args['Z']/args['A'] * ( E_term1 @ (K @ jnp.diag(E_plus)) + E_term2 @ (K @ jnp.diag(E_minus)) )
        
        nu = args['nu'].reshape(1, -1)
        ns = (K.T @ zeroth_moment(V, grid)).reshape(1, -1)
        Mv  = maxwellianv(grid, args['A']).reshape(1, -1)
        
        gamma = args['flux_out']
        gamma *= jnp.maximum(flux_source_shape_func(grid), 0.0).reshape(1, -1)
        
        collision_term1 = (V @ Mv.T) @ (nu * ns + gamma) * grid.dv
        collision_term2 = V @ V.T @ K * nu * grid.dv
        
        return -v_flux_diff - E_flux + collision_term1 - collision_term2 


    def step_S(self, t, ys, args):
        """
        Perform S-step of the KSL splitting (evolve S component).
        
        Args:
            t: Current time
            ys: Current state (XSV decomposition for each species)
            args: Additional arguments
            
        Returns:
            Updated state after S-step
        """
        flux_out = self.ion_flux_out(ys)
        S_of = lambda X, S, V: S
        args_of = lambda sp: {**args, 'X': ys[sp][0], 'V': ys[sp][2],
                              'Z': self.Zs[sp], 'A': self.As[sp], 'nu': self.nus[sp], 
                              'flux_out': flux_out}

        def step_Ss_with_E_RHS(Ss):
            # HACKATHON: E = ...
            E = solve_poisson_XSV(Ss, ys, self.grids, args['bcs'], self.plasma)
            args['E'] = E
            return { sp: self.S_step_single_species_RHS(Ss[sp], self.grids[sp], 
                                                                 {**args_of(sp)})
                    for sp in SPECIES }


        Ss = rk1({ sp: S_of(*ys[sp]) for sp in SPECIES }, step_Ss_with_E_RHS, args['dt'])

        def XSV_of(S, y):
            X, _, V = y
            return (X, S, V)
        return { sp: XSV_of(Ss[sp], ys[sp]) for sp in SPECIES }


    def S_step_single_species_RHS(self, S, grid, args):
        """
        Compute right-hand side for S-step for a single species.
        
        Args:
            S: Current S matrix
            grid: Phase space grid for the species
            args: Arguments containing X, V, E, collision parameters, etc.
            
        Returns:
            Time derivative of S matrix
        """
        X, V = args['X'], args['V']
        v = grid.vs
        r = self.r

        v_plus_matrix = V @ jnp.diag(jnp.where(v > 0, v, 0.0)) @ V.T * grid.dv
        v_minus_matrix = V @ jnp.diag(jnp.where(v < 0, v, 0.0)) @ V.T * grid.dv

        K = self.apply_K_bcs((X.T @ S).T, V, grid, n_ghost_cells=1)
        K_diff_left = jnp.diff(K[:, :-1], axis=1) / grid.dx
        K_left_matrix = X @ K_diff_left.T * grid.dx
        K_diff_right = jnp.diff(K[:, 1:], axis=1) / grid.dx
        K_right_matrix = X @ K_diff_right.T * grid.dx

        v_term = K_left_matrix @ v_plus_matrix.T + K_right_matrix @ v_minus_matrix.T

        # HACKATHON: add E*df/dv term here
        # You'll need to implement:
        # 1. Compute upwinded <X, E^\pm X> matrices based on sign of Z/A * E
        # 2. Compute upwind dV/dv matrices <V, E^\pm V>
        # 3. Compute the projected E*df/dv flux term

        # HACKATHON: add collision terms and flux source terms here
        # You'll need to implement:
        # 1. Compute density n
        # 2. BGK collision operator: nu * (M - f) where M is Maxwellian with density n
        # 3. Flux source terms for particle injection
        # See collision_frequency_shape_func and flux_source_shape_func in collisions_and_sources.py
        
        E = args['E']
        Dp_V = jnp.diff(jnp.concatenate([jnp.zeros((V.shape[0], 1)), V], axis=1), axis=1) / grid.dv
        Dm_V = jnp.diff(jnp.concatenate([V, jnp.zeros((V.shape[0], 1))], axis=1), axis=1) / grid.dv
        E_plus  = jnp.where(args['Z'] * E > 0, E, 0.)
        E_minus = jnp.where(args['Z'] * E < 0, E, 0.)
        V_term1 = V @ Dp_V.T * grid.dv
        V_term2 = V @ Dm_V.T * grid.dv
        X_term1 = X @ (X @ jnp.diag(E_plus)).T * grid.dx
        X_term2 = X @ (X @ jnp.diag(E_minus)).T * grid.dx
        E_flux = self.plasma.omega_c_tau * args['Z']/args['A'] * ( X_term1 @ S @ V_term1.T + X_term2 @ S @ V_term2.T )
        gamma = args['flux_out']
        gamma *= jnp.maximum(flux_source_shape_func(grid), 0.0).reshape(1, -1)
        nu    = args['nu'].reshape(1, -1)
        ns    = (X.T @ S @ zeroth_moment(V, grid)).reshape(1, -1)
        Mv    = maxwellianv(grid, args['A']).reshape(1, -1)
        # print(K.shape, ns.shape, gamma.shape, Mv.shape)
        collision_term1 = X @ (nu * ns + gamma).T * grid.dx @ Mv @ V.T * grid.dv
        collision_term2 = X @ (X * nu).T @ S @ (V @ V.T) * grid.dx * grid.dv
        
        return v_term + E_flux - collision_term1 + collision_term2


    def step_L(self, t, ys, args):
        """
        Perform L-step of the KSL splitting (evolve S V component).
        
        Args:
            t: Current time
            ys: Current state (XSV decomposition for each species)
            args: Additional arguments
            
        Returns:
            Updated state after L-step
        """
        L_of = lambda X, S, V: S @ V
        flux_out = self.ion_flux_out(ys)
        args_of = lambda sp: {**args, 'X': ys[sp][0], 'Z': self.Zs[sp], 'A': self.As[sp], 
                              'nu': self.nus[sp], 'flux_out': flux_out}

        def step_Ls_with_E_RHS(Ls):
            # HACKATHON: E = ...
            E = solve_poisson_XL(Ls, ys, self.grids, args['bcs'], self.plasma)
            args['E'] = E
            return { sp: self.L_step_single_species_RHS(Ls[sp], self.grids[sp], 
                                                                 {**args_of(sp)})
                    for sp in SPECIES }
        
        Ls = rk1({ sp: L_of(*ys[sp]) for sp in SPECIES }, step_Ls_with_E_RHS, args['dt'])

        def XSV_of(L, y, grid):
            X, _, _ = y
            (Vt, St) = jnp.linalg.qr(L.T)
            return (X, St.T * grid.dv**0.5, Vt.T / grid.dv**0.5)

        return { sp: XSV_of(Ls[sp], ys[sp], self.grids[sp]) for sp in SPECIES }


    def L_step_single_species_RHS(self, L, grid, args):
        """
        Compute right-hand side for L-step for a single species.
        
        Args:
            L: Current L matrix (S V)
            grid: Phase space grid for the species
            args: Arguments containing X, E, collision parameters, etc.
            
        Returns:
            Time derivative of L matrix
        """
        X = args['X']
        v = grid.vs
        r = self.r

        Vt, St = jnp.linalg.qr(L.T)
        S = St.T * grid.dv**0.5
        V = Vt.T / grid.dv**0.5

        K = self.apply_K_bcs((X.T @ S).T, V, grid, n_ghost_cells=1)
        K_diff_left = jnp.diff(K[:, :-1], axis=1) / grid.dx
        K_left_matrix = X @ K_diff_left.T * grid.dx
        K_diff_right = jnp.diff(K[:, 1:], axis=1) / grid.dx
        K_right_matrix = X @ K_diff_right.T * grid.dx
        
        v_plus = jnp.where(v > 0, v, 0.0)
        v_minus = jnp.where(v < 0, v, 0.0)
        v_flux = jnp.atleast_2d(v_plus) * (K_left_matrix @ V) + jnp.atleast_2d(v_minus) * (K_right_matrix @ V)
        
        # HACKATHON: add E*df/dv term here
        # You'll need to implement:
        # 1. Compute upwinded <X, E^\pm X> matrices based on sign of Z/A * E
        # 2. Apply zero Dirichlet boundaries to L
        # 3. Compute the flux divergence using the slope_limited_flux_divergence function

        # HACKATHON: add collision terms and flux source terms here
        # You'll need to implement:
        # 1. Compute density n
        # 2. BGK collision operator: nu * (M - f) where M is Maxwellian with density n
        # 3. Flux source terms for particle injection
        # See collision_frequency_shape_func and flux_source_shape_func in collisions_and_sources.py
     
        Dp_L = jnp.diff(jnp.concatenate([jnp.zeros((L.shape[0], 1)), L], axis=1), axis=1) / grid.dv
        Dm_L = jnp.diff(jnp.concatenate([L, jnp.zeros((L.shape[0], 1))], axis=1), axis=1) / grid.dv
        
        E = args['E']
        E_plus  = jnp.where(args['Z'] * E>0, E, 0.)
        E_minus = jnp.where(args['Z'] * E<0, E, 0.)
        
        E_flux = self.plasma.omega_c_tau * args['Z'] / args['A'] * X @ (X @ jnp.diag(E_plus)).T * grid.dx @ Dp_L 
        E_flux += self.plasma.omega_c_tau * args['Z'] / args['A'] * X @ (X @ jnp.diag(E_minus)).T * grid.dx @ Dm_L
        
        nu = args['nu'].reshape(-1, 1)
        gamma = args['flux_out']
        gamma *= jnp.maximum(flux_source_shape_func(grid), 0.0).reshape(-1, 1)
        ns = X.T @ S @ zeroth_moment(V, grid).reshape(-1, 1)
        Mv = maxwellianv(grid, args['A']).reshape(1, -1)
        
        collision_term1 = X @ ( ns * nu + gamma ) @ Mv * grid.dx
        collision_term2 = X @ (nu*X.T) @ L * grid.dx

        return -v_flux - E_flux + collision_term1 - collision_term2


    def ion_flux_out(self, ys):
        """
        Compute ion flux leaving the domain boundaries.
        
        Args:
            ys: Current state (XSV decomposition for each species)
            
        Returns:
            Net ion flux out of the domain
        """
        X, S, V = ys['ion']
        K = (X.T @ S).T
        v = self.grids['ion'].vs
        v_vec = V @ self.grids['ion'].vs * self.grids['ion'].dv
        flux_out = -(K[:, 0]).T @ v_vec + (K[:, -1]).T @ v_vec
        return flux_out


    def apply_K_bcs(self, K, V, grid, n_ghost_cells):
        """
        Apply boundary conditions to K matrix in velocity space.
        
        Args:
            K: K matrix (X^T S)
            V: V matrix
            grid: Phase space grid
            n_ghost_cells: Number of ghost cells to add
            
        Returns:
            K matrix with boundary conditions applied
        """
        r = K.shape[0]
        v = grid.vs
        V_leftgoing_matrix = V @ jnp.diag(jnp.where(v < 0, 1.0, 0.0)) @ V.T * grid.dv
        V_rightgoing_matrix = V @ jnp.diag(jnp.where(v > 0, 1.0, 0.0)) @ V.T * grid.dv

        if self.boundary_type == 'AbsorbingWall':
            # HACKATHON: implement absorbing wall boundary conditions
            # for either 1 or 2 ghost cells
            # raise NotImplementedError("HACKATHON: Implement absorbing wall BCs for DLR solver")
            left_ghost  = K[:, 0] @ V_leftgoing_matrix
            right_ghost = K[:, -1] @ V_rightgoing_matrix
            if n_ghost_cells == 1:
                return jnp.concatenate([
                    left_ghost.reshape(r, 1),
                    K,
                    right_ghost.reshape(r, 1)
                ], axis=1)
            elif n_ghost_cells == 2:
                return jnp.concatenate([
                    left_ghost.reshape(r, 1),
                    left_ghost.reshape(r, 1),
                    K,
                    right_ghost.reshape(r, 1),
                    right_ghost.reshape(r, 1)
                ], axis=1)
        elif self.boundary_type == 'Periodic':
            if n_ghost_cells == 1:
                return jnp.concatenate([
                    jnp.atleast_2d(K[:, -1]).T,
                    K,
                    jnp.atleast_2d(K[:, 0]).T,
                ], axis=1)
            elif n_ghost_cells == 2:
                return jnp.concatenate([
                    K[:, [-2, -1]],
                    K, 
                    K[:, [0, 1]],
                ], axis=1)



class Stepper(Euler):
    """
    Custom stepper for the DLRA solver.
    
    Needed to integrate with the diffrax library for affordable reverse-mode differentiation
    """

    def step(self, terms, t0, t1, y0, args, solver_state, made_jump):
        """
        Perform one step of the custom stepper.
        
        Args:
            terms: ODE terms
            t0: Initial time
            t1: Final time
            y0: Initial state
            args: Additional arguments
            solver_state: Solver state (unused)
            made_jump: Jump indicator (unused)
            
        Returns:
            Tuple of (new_state, new_solver_state, dense_info, aux_stats, result)
        """
        del solver_state, made_jump
        y1 = terms.vf(t0, y0, args | {"dt": t1 - t0})
        dense_info = dict(y0=y0, y1=y1)
        return y1, None, dense_info, None, RESULTS.successful
